"""Polymorphic search backends for :class:`SearchController`.

Two strategies exist because the composer-paste code-search has
two very different execution paths that used to live as
free ``_search_with_rg`` / ``_search_with_python`` functions in
:mod:`ember_code.backend.server_search`:

* :class:`RgSearchStrategy` — shells out to ``rg`` when it's on
  ``PATH``. Fast, parallel, gitignore-aware.
* :class:`PythonWalkSearchStrategy` — pure-Python ``os.walk``
  fallback for machines without ``rg``.

Both subclasses expose the same :meth:`SearchStrategy.search`
contract and return a :class:`SearchCodeResult`, so the
controller composes one instance (chosen once at construction
via :meth:`SearchStrategy.pick`) and calls it uniformly — no
per-call ``shutil.which`` branch, no dispatch dict.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from ember_code.backend.schemas_search import SearchCodeMatch, SearchCodeResult

_DEFAULT_FALLBACK_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        "target",
        ".idea",
        ".vscode",
    }
)


class SearchStrategy(ABC):
    """Polymorphic base for a project-wide substring-search backend."""

    @abstractmethod
    def search(
        self,
        project_root: Path,
        snippet: str,
        snippet_lines: int,
        max_results: int,
    ) -> SearchCodeResult:
        """Return exact-substring matches for ``snippet`` under ``project_root``.

        ``snippet_lines`` is the number of newline-separated lines in
        the snippet (already computed by the controller); strategies
        use it to fill :attr:`SearchCodeMatch.end_line`. ``max_results``
        is the hard cap — strategies stop appending once
        :meth:`SearchCodeResult.append` returns ``True``.
        """

    @classmethod
    def pick(cls) -> SearchStrategy:
        """Return the best strategy for the current environment.

        Called once by :class:`SearchController` at construction time
        so the ``shutil.which`` branch runs on a per-controller basis,
        not per search call.
        """
        rg = shutil.which("rg")
        if rg:
            return RgSearchStrategy(rg)
        return PythonWalkSearchStrategy()


class RgSearchStrategy(SearchStrategy):
    """``rg``-backed search — parallel, gitignore-aware, fast."""

    def __init__(self, rg_path: str) -> None:
        self._rg_path = rg_path

    def search(
        self,
        project_root: Path,
        snippet: str,
        snippet_lines: int,
        max_results: int,
    ) -> SearchCodeResult:
        # TODO(follow-up): switch to ``rg --json`` — the current
        # ``split(':', 2)`` parse breaks on Windows drive letters
        # (``C:\path:12:preview``). Flagged as design-phase, not a
        # blocker for the OOP refactor.
        cmd = [
            self._rg_path,
            "--fixed-strings",
            "--line-number",
            "--no-heading",
            "--color=never",
            "--max-count=5",
            "--max-filesize=2M",
            "--multiline",
            "--multiline-dotall",
            snippet,
            str(project_root),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return SearchCodeResult.timed_out()
        result = SearchCodeResult()
        for raw_line in (proc.stdout or "").splitlines():
            parts = raw_line.split(":", 2)
            if len(parts) < 3:
                continue
            abs_path, line_str, preview = parts
            try:
                line = int(line_str)
            except ValueError:
                continue
            try:
                rel = str(Path(abs_path).resolve().relative_to(project_root))
            except ValueError:
                rel = abs_path
            match = SearchCodeMatch(
                path=rel,
                line=line,
                end_line=line + snippet_lines - 1,
                preview=preview.strip(),
            )
            if result.append(match, max_results):
                break
        return result


class PythonWalkSearchStrategy(SearchStrategy):
    """Python-side ``os.walk`` fallback for hosts without ``rg``.

    Skips a curated set of noisy dependency / build directories
    (see the ``skip_dirs`` constructor arg) so a fresh
    ``node_modules`` doesn't torpedo the fallback path.
    """

    def __init__(
        self,
        skip_dirs: frozenset[str] = _DEFAULT_FALLBACK_SKIP_DIRS,
    ) -> None:
        self._skip_dirs = skip_dirs

    def search(
        self,
        project_root: Path,
        snippet: str,
        snippet_lines: int,
        max_results: int,
    ) -> SearchCodeResult:
        result = SearchCodeResult()
        first_line = snippet.splitlines()[0]
        for dirpath, dirnames, filenames in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in self._skip_dirs]
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    if p.stat().st_size > 2 * 1024 * 1024:
                        continue
                    text = p.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeDecodeError):
                    continue
                idx = text.find(snippet)
                if idx < 0:
                    continue
                line_no = text.count("\n", 0, idx) + 1
                try:
                    rel = str(p.resolve().relative_to(project_root))
                except ValueError:
                    rel = str(p)
                match = SearchCodeMatch(
                    path=rel,
                    line=line_no,
                    end_line=line_no + snippet_lines - 1,
                    preview=first_line.strip(),
                )
                if result.append(match, max_results):
                    return result
        return result
