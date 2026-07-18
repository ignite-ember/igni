"""Search tools — Grep (ripgrep) and Glob (pathlib) wrappers."""

import subprocess
from pathlib import Path

from agno.tools import Toolkit

_RG_TIMEOUT_S = 30
_MAX_OUTPUT_CHARS = 10_000
_SKIP_DIRS = frozenset(
    {"__pycache__", ".git", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}
)


def _run_rg(cmd: list[str]) -> tuple[bool, str]:
    """Run ripgrep and return ``(ok, output)``.

    ``ok`` is False only for hard failures (rg missing, timeout,
    non-1 stderr). rg's exit code 1 (no matches) is treated as a
    normal outcome, since callers stringify to the agent either way.
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_RG_TIMEOUT_S)
    except FileNotFoundError:
        return False, "Error: ripgrep (rg) is not installed. Install it: brew install ripgrep"
    except subprocess.TimeoutExpired:
        return False, f"Error: Search timed out after {_RG_TIMEOUT_S} seconds."

    if result.returncode in (0, 1):
        return True, result.stdout
    return False, f"Error running ripgrep: {result.stderr}"


class GrepTools(Toolkit):
    """Content search using ripgrep (rg)."""

    def __init__(self, base_dir: str | None = None, **kwargs):
        super().__init__(name="ember_grep", **kwargs)
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.register(self.grep)
        self.register(self.grep_files)
        self.register(self.grep_count)

    def _resolve(self, path: str) -> str:
        return str(self.base_dir / path) if path else str(self.base_dir)

    def grep(
        self,
        pattern: str,
        path: str = "",
        glob: str = "",
        file_type: str = "",
        context_lines: int = 0,
        max_results: int = 50,
    ) -> str:
        """Search file contents with regex using ripgrep.

        Args:
            pattern: Regex pattern to search for.
            path: Directory or file to search in. Defaults to project root.
            glob: Glob pattern to filter files (e.g., "*.py").
            file_type: File type filter (e.g., "py", "js").
            context_lines: Number of context lines around matches.
            max_results: Maximum results to return.

        Returns:
            Matching lines with file paths and line numbers.
        """
        cmd = ["rg", "--no-heading", "-n"]

        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])
        if glob:
            cmd.extend(["--glob", glob])
        if file_type:
            cmd.extend(["--type", file_type])

        cmd.extend(["-m", str(max_results), pattern, self._resolve(path)])

        ok, out = _run_rg(cmd)
        if not ok:
            return out
        return (out[:_MAX_OUTPUT_CHARS] if out else "") or "No matches found."

    def grep_files(self, pattern: str, path: str = "", glob: str = "") -> str:
        """Search and return only matching file paths.

        Args:
            pattern: Regex pattern to search for.
            path: Directory to search in.
            glob: Glob pattern to filter files.

        Returns:
            List of file paths containing matches.
        """
        cmd = ["rg", "--files-with-matches"]
        if glob:
            cmd.extend(["--glob", glob])
        cmd.extend([pattern, self._resolve(path)])

        ok, out = _run_rg(cmd)
        if not ok:
            return out
        return out or "No matching files found."

    def grep_count(self, pattern: str, path: str = "") -> str:
        """Return match counts per file.

        Args:
            pattern: Regex pattern to search for.
            path: Directory to search in.

        Returns:
            File paths with match counts.
        """
        cmd = ["rg", "--count", pattern, self._resolve(path)]
        ok, out = _run_rg(cmd)
        if not ok:
            return out
        return out or "No matches found."


class GlobTools(Toolkit):
    """File pattern matching using pathlib."""

    def __init__(self, base_dir: str | None = None, **kwargs):
        super().__init__(name="ember_glob", **kwargs)
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.register(self.glob_files)

    def glob_files(self, pattern: str, path: str = "", max_results: int = 100) -> str:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "src/**/*.ts").
            path: Subdirectory to search in. Defaults to project root.
            max_results: Maximum number of results.

        Returns:
            List of matching file paths, sorted by modification time.
        """
        search_dir = self.base_dir / path if path else self.base_dir

        if not search_dir.exists():
            return f"Error: Directory not found: {search_dir}"

        matches: list[Path] = []
        for p in search_dir.glob(pattern):
            if p.is_file() and not (_SKIP_DIRS & set(p.parts)):
                matches.append(p)
                if len(matches) >= max_results:
                    break

        # Sort by modification time (newest first)
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        if not matches:
            return f"No files matching '{pattern}' found in {search_dir}"

        lines = [str(p.relative_to(self.base_dir)) for p in matches]
        return "\n".join(lines)
