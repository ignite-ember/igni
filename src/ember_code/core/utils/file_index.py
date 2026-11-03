"""File index — cached project file listing with fuzzy matching."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

# Directories to skip when git is not available
_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".eggs",
    "*.egg-info",
}


def _git_ls_files(project_dir: Path) -> list[str]:
    """Run git ls-files and return relative paths."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607 — git from PATH, as the user runs it
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return sorted(result.stdout.strip().splitlines())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return []


def _fallback_list(project_dir: Path) -> list[str]:
    """Walk the directory tree, skipping common non-project dirs."""
    files: list[str] = []
    for path in project_dir.rglob("*"):
        if not path.is_file():
            continue
        # Skip ignored directories
        parts = path.relative_to(project_dir).parts
        if any(p in _IGNORE_DIRS or p.endswith(".egg-info") for p in parts):
            continue
        files.append(str(path.relative_to(project_dir)))
    return sorted(files)


def _score_match(query: str, path: str) -> int | None:
    """Score a fuzzy subsequence match. Returns None if no match.

    Higher score = better match. Rewards:
    - Contiguous character runs
    - Matches at path-segment boundaries (after / or .)
    - Matches at the start of the filename
    """
    query_lower = query.lower()
    path_lower = path.lower()

    # Quick check: all query chars must exist in path
    qi = 0
    for ch in path_lower:
        if qi < len(query_lower) and ch == query_lower[qi]:
            qi += 1
    if qi < len(query_lower):
        return None

    # Score the match
    score = 0
    qi = 0
    prev_matched = False
    for pi, ch in enumerate(path_lower):
        if qi >= len(query_lower):
            break
        if ch == query_lower[qi]:
            qi += 1
            # Contiguous bonus
            if prev_matched:
                score += 5
            else:
                score += 1
            # Boundary bonus: start of path, after / or ., after _/-
            if pi == 0 or path_lower[pi - 1] in "/._-":
                score += 3
            prev_matched = True
        else:
            prev_matched = False

    # Bonus for shorter paths (less noise)
    score -= len(path) // 10

    # Bonus for matching the filename portion
    filename = path.rsplit("/", 1)[-1].lower()
    if query_lower in filename:
        score += 10

    return score


class FileIndex:
    """Cached project file listing with fuzzy matching."""

    def __init__(self, project_dir: Path | None = None) -> None:
        self._project_dir = project_dir or Path.cwd()
        self._files: list[str] = []
        self._loaded = False

    async def ensure_loaded(self) -> None:
        """Load the file list (runs git ls-files in a thread)."""
        if self._loaded:
            return
        files = await asyncio.to_thread(_git_ls_files, self._project_dir)
        if not files:
            files = await asyncio.to_thread(_fallback_list, self._project_dir)
        self._files = files
        self._loaded = True

    def refresh_sync(self) -> None:
        """Synchronously refresh the file list."""
        files = _git_ls_files(self._project_dir)
        if not files:
            files = _fallback_list(self._project_dir)
        self._files = files
        self._loaded = True

    def match(self, query: str, limit: int = 100) -> list[str]:
        """Return files matching query via fuzzy subsequence match."""
        matches, _ = self.match_with_total(query, limit=limit)
        return matches

    def match_with_total(self, query: str, limit: int = 100) -> tuple[list[str], int]:
        """Same as :meth:`match`, but also returns how many matches
        existed before truncation. The FE uses ``total`` to tell the
        user when the visible list is a slice of a larger result and
        they should type more to narrow it down.
        """
        if not self._files:
            return [], 0
        if not query:
            return self._files[:limit], len(self._files)
        scored: list[tuple[int, str]] = []
        for path in self._files:
            s = _score_match(query, path)
            if s is not None:
                scored.append((s, path))
        scored.sort(key=lambda x: -x[0])
        return [p for _, p in scored[:limit]], len(scored)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def file_count(self) -> int:
        return len(self._files)
