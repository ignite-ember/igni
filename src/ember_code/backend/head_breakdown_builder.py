"""Builders that assemble CodeIndex panel views.

Extracted out of :mod:`ember_code.backend.server_codeindex` — the
old controller carried an 89-line :meth:`head_breakdown` method
that mixed four concerns inline: shelling to ``git ls-files``,
tallying extensions into a :class:`Counter`, shelling to
``git log``, and reading per-commit chroma directory sizes off
disk. Each concern is now a small class here so the controller
becomes composition, not script.

Classes:

* :class:`HeadBreakdownBuilder` — top-level orchestrator that
  the controller instantiates once and calls
  :meth:`HeadBreakdownBuilder.build` on. Owns
  ``project_dir`` + ``code_index`` as instance state.
* :class:`GitLsFilesRunner` — thread-offloaded ``git ls-files``
  wrapper. Returns the tracked-paths list, or an error string.
* :class:`GitRecentLogRunner` — thread-offloaded ``git log``
  wrapper that parses tab-separated columns into
  :class:`CommitBreakdown` rows.
* :class:`LanguageHistogram` — pure ``Counter`` over the
  extension slice of each tracked path.
* :class:`BranchIndexInventory` — walks the manifest's commits
  dict and computes per-commit chroma dir sizes on disk (used
  by :meth:`CodeIndexController.status`, kept here so the free
  ``_dir_size`` helper becomes an owned :func:`staticmethod`).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.backend.schemas_codeindex_rpc import (
    BranchIndexEntry,
    CodeIndexHeadBreakdown,
    CommitBreakdown,
    LangCount,
)
from ember_code.core.code_index.paths import commit_chroma_path

if TYPE_CHECKING:
    from ember_code.core.code_index.index import CodeIndex
    from ember_code.core.code_index.manifest import ManifestState

logger = logging.getLogger(__name__)


class GitLsFilesRunner:
    """Thread-offloaded ``git ls-files`` wrapper.

    Kept as a class rather than a free function so the
    ``project_dir`` constructor arg + the ``run()`` return type
    ((tracked_paths, error)) live together — earlier attempts
    used a bare tuple returned from a helper function and a
    reviewer had to trace the call site to know which slot was
    which.
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    async def run(self) -> tuple[list[str], str]:
        """Return ``(tracked_paths, error)``. On success ``error``
        is empty; on failure ``tracked_paths`` is empty."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "ls-files"],
                cwd=self._project_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return [], "git not available"
        if result.returncode != 0:
            return [], result.stderr.strip() or "git ls-files failed"
        return [p for p in result.stdout.splitlines() if p], ""


class GitRecentLogRunner:
    """Thread-offloaded ``git log -5`` wrapper that parses the
    tab-separated ``%H\\t%h\\t%s\\t%cr`` format into typed
    :class:`CommitBreakdown` rows."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    async def run(self, indexed_shas: set[str]) -> list[CommitBreakdown]:
        """Return the last five commits, tagging each with
        whether its full SHA appears in ``indexed_shas``."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "log", "-5", "--pretty=format:%H%x09%h%x09%s%x09%cr"],
                cwd=self._project_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        commits: list[CommitBreakdown] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            full, short, subj, when = parts[:4]
            commits.append(
                CommitBreakdown(
                    sha=short,
                    full_sha=full,
                    subject=subj,
                    when=when,
                    indexed=full in indexed_shas,
                )
            )
        return commits


class LanguageHistogram:
    """Extension-count tally over a list of tracked file paths.

    Written as a class so the top-10 slicing rule lives together
    with the counter — earlier prototypes returned a raw
    :class:`Counter` and callers had to remember to ``.most_common(10)``
    themselves."""

    OTHER_LABEL = "(other)"
    TOP_N = 10

    def __init__(self, tracked: list[str]) -> None:
        self._tracked = tracked

    def top(self) -> list[LangCount]:
        """Return the most-common ``TOP_N`` extensions as typed
        wire entries. Files with no extension collapse into a
        single ``(other)`` bucket."""
        counts: Counter[str] = Counter()
        for path in self._tracked:
            counts[self._extension_of(path)] += 1
        return [LangCount(ext=ext, count=n) for ext, n in counts.most_common(self.TOP_N)]

    @classmethod
    def _extension_of(cls, path: str) -> str:
        i = path.rfind(".")
        if 0 < i < len(path) - 1:
            return path[i + 1 :].lower()
        return cls.OTHER_LABEL


class BranchIndexInventory:
    """Walks the manifest's ``commits`` dict and computes per-
    commit chroma directory sizes on disk.

    Used by :meth:`CodeIndexController.status` to build the
    "branches indexed" table + the total index size. Owns the
    filesystem-walk helper as a :func:`staticmethod` so the free
    ``_dir_size`` at module scope disappears from the codebase.
    """

    def __init__(self, code_index: CodeIndex) -> None:
        self._code_index = code_index

    def build(self, state: ManifestState) -> tuple[list[BranchIndexEntry], int]:
        """Return ``(sorted_entries, total_size_bytes)``. Entries
        are sorted newest-``last_used_at`` first, matching the
        panel's row order."""
        entries: list[BranchIndexEntry] = []
        total = 0
        for sha, info in state.commits.items():
            chroma_dir = commit_chroma_path(
                self._code_index.project,
                sha,
                data_dir=self._code_index.data_dir,
            )
            size = self._dir_size(chroma_dir)
            total += size
            entries.append(
                BranchIndexEntry(
                    sha=sha,
                    is_head=sha == state.head,
                    size_bytes=size,
                    last_used_at=info.last_used_at,
                    branch_refs=list(info.branch_refs),
                )
            )
        entries.sort(key=lambda c: c.last_used_at, reverse=True)
        return entries, total

    @staticmethod
    def _dir_size(p: Path) -> int:
        try:
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        except OSError:
            return 0


class HeadBreakdownBuilder:
    """Assembles :class:`CodeIndexHeadBreakdown` from git shell-
    outs + the code index's own manifest and head-stats.

    Composition entry point: the controller does a one-liner
    ``await HeadBreakdownBuilder(project_dir, code_index).build()``.
    """

    def __init__(self, project_dir: Path, code_index: CodeIndex) -> None:
        self._project_dir = project_dir
        self._code_index = code_index
        self._ls_files = GitLsFilesRunner(project_dir)
        self._log = GitRecentLogRunner(project_dir)

    async def build(self) -> CodeIndexHeadBreakdown:
        """Do the three async steps in order: list tracked files,
        tally extensions, pull recent commits, and read the
        head-stats for the indexed-per-language counts."""
        tracked, error = await self._ls_files.run()
        if error:
            return CodeIndexHeadBreakdown(
                file_count=0,
                languages=[],
                recent_commits=[],
                files_indexed=0,
                languages_indexed={},
                error=error,
            )

        top_langs = LanguageHistogram(tracked).top()

        state = self._code_index.manifest.load()
        indexed_shas = set(state.commits.keys())
        recent_commits = await self._log.run(indexed_shas)

        files_indexed, languages_indexed = await self._head_stats(state.head or "")
        return CodeIndexHeadBreakdown(
            file_count=len(tracked),
            languages=top_langs,
            recent_commits=recent_commits,
            files_indexed=files_indexed,
            languages_indexed=languages_indexed,
        )

    async def _head_stats(self, head_sha: str) -> tuple[int, dict[str, int]]:
        if not head_sha:
            return 0, {}
        try:
            head = await self._code_index.head_stats(head_sha)
        except Exception as exc:
            logger.debug("head_stats failed: %s", exc)
            return 0, {}
        return head.files_indexed, dict(head.languages_indexed)
