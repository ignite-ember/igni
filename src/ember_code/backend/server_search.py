"""Composer-paste code search: exact-substring lookup across the project.

Extracted from :mod:`ember_code.backend.server`. One free
function — :func:`search_code` — used by the composer's paste
handler to answer "where does this snippet live in the repo?"
so the FE can decorate the message with file refs.

Strategy: ``rg`` when present (parallel, gitignore-aware), else
a Python-side ``os.walk`` fallback. Match mode is exact
substring — no normalisation, no fuzzy — and multi-line
snippets become a single literal pattern.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.backend.server_helpers import _search_code_cache_put

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer


class SearchCodeMatch(BaseModel):
    """One hit in :attr:`SearchCodeResult.matches`."""

    path: str
    line: int
    end_line: int
    preview: str


class SearchCodeResult(BaseModel):
    """Wire shape for :func:`search_code` — composer paste
    decoration. ``truncated`` is True whenever the max-results cap
    was hit (or a search timed out) so the FE can render "and N
    more…". ``error`` is empty on success paths."""

    matches: list[SearchCodeMatch] = []
    truncated: bool = False
    error: str = ""


_FALLBACK_SKIP_DIRS: frozenset[str] = frozenset(
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


def search_code(
    backend: "BackendServer", snippet: str, max_results: int = 20
) -> SearchCodeResult:
    """Find exact-substring occurrences of ``snippet`` across the
    project. Used by the composer's paste handler — when the user
    pastes code, the FE asks "where does this live?" so it can
    decorate the message with refs.

    Strategy:
      - Use ``rg`` if available (parallel, gitignore-aware, fast).
      - Otherwise walk the project with Python, skipping noisy dirs.

    Match mode is **exact substring** — no normalisation, no
    fuzzy. Multi-line snippets become a single literal pattern.

    Returns ``{matches: [{path, line, end_line, preview}], truncated: bool}``.
    ``path`` is project-relative. ``line`` is the start line of
    the match; ``end_line`` is computed from the snippet itself
    (start + newline count) so the pill can label a 5-line paste
    as ``71-75`` instead of just ``71`` — rg's ``--multiline``
    only reports the start line and the FE can't derive the end
    without knowing the snippet here on the BE.

    Repeated pastes of the same snippet (re-pasting after an edit,
    the model echoing code back) hit a small in-process cache so
    only the first lookup pays for the rg spawn.
    """
    snippet = (snippet or "").strip()
    if len(snippet) < 5:
        return SearchCodeResult()

    # ── Result cache ──
    # Bounded to a few dozen entries; rotates by reinsertion order
    # (Python dicts preserve insertion order). The key includes
    # the project root so switching directories doesn't serve
    # stale results.
    project_root = Path(backend._session.project_dir).resolve()
    cache_key = hashlib.sha1(
        f"{project_root}\0{max_results}\0{snippet}".encode("utf-8", "ignore")
    ).hexdigest()
    cache: dict[str, SearchCodeResult] = getattr(backend, "_search_code_cache", None) or {}
    if not hasattr(backend, "_search_code_cache"):
        backend._search_code_cache = cache
    cached = cache.get(cache_key)
    if cached is not None:
        # Move to MRU position.
        cache.pop(cache_key, None)
        cache[cache_key] = cached
        return cached

    # Used below for the end_line calculation. ``rg --multiline``
    # emits one row per match (start line only), so we derive the
    # end from the snippet structure itself.
    snippet_lines = snippet.count("\n") + 1

    rg = shutil.which("rg")
    if rg:
        payload = _search_with_rg(rg, project_root, snippet, snippet_lines, max_results)
    else:
        payload = _search_with_python(project_root, snippet, snippet_lines, max_results)
    _search_code_cache_put(cache, cache_key, payload)
    return payload


def _search_with_rg(
    rg: str,
    project_root: Path,
    snippet: str,
    snippet_lines: int,
    max_results: int,
) -> SearchCodeResult:
    """rg-based path. Fast, gitignore-aware."""
    # --fixed-strings: literal pattern (no regex)
    # --line-number: include line numbers
    # --no-heading: machine-friendly output
    # --multiline: required for snippets with newlines
    # --max-count: per-file cap
    # Newlines inside the snippet require --multiline+--multiline-dotall.
    cmd = [
        rg,
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
        return SearchCodeResult(matches=[], truncated=True, error="search timed out")
    results: list[SearchCodeMatch] = []
    truncated = False
    for raw_line in (proc.stdout or "").splitlines():
        # Format: "/abs/path:LINE:preview"
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
        results.append(
            SearchCodeMatch(
                path=rel,
                line=line,
                end_line=line + snippet_lines - 1,
                preview=preview.strip(),
            )
        )
        if len(results) >= max_results:
            truncated = True
            break
    return SearchCodeResult(matches=results, truncated=truncated)


def _search_with_python(
    project_root: Path,
    snippet: str,
    snippet_lines: int,
    max_results: int,
) -> SearchCodeResult:
    """Python fallback for when ``rg`` isn't on PATH.

    Walk text-ish files, scan line-by-line for the first line of
    the snippet, then verify the full snippet at that offset.
    """
    results: list[SearchCodeMatch] = []
    truncated = False
    first_line = snippet.splitlines()[0]
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in _FALLBACK_SKIP_DIRS]
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
            results.append(
                SearchCodeMatch(
                    path=rel,
                    line=line_no,
                    end_line=line_no + snippet_lines - 1,
                    preview=first_line.strip(),
                )
            )
            if len(results) >= max_results:
                truncated = True
                return SearchCodeResult(matches=results, truncated=truncated)
    return SearchCodeResult(matches=results, truncated=truncated)
