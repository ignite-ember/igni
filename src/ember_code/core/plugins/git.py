"""Thin subprocess wrapper around the ``git`` CLI.

Used by :class:`PluginInstaller` to clone / fetch / reset plugin
repos. We shell out to ``git`` rather than pull in a pure-Python git
library: zero new dependencies, errors are legible (verbatim git
stderr), and SSH / HTTPS / file URLs all work via the user's existing
git configuration. The trade-off — requiring ``git`` on PATH — is
checked once via :meth:`GitClient.is_available` and surfaced as a
clear error before any install command runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """A ``git`` command failed, timed out, or ``git`` is unavailable."""


class GitClient:
    """Subprocess wrapper around the ``git`` CLI.

    Stateless beyond the timeout. Every call shells out fresh; no
    long-lived git process. Errors are raised as :class:`GitError`
    with verbatim stderr so the user sees the same message git would
    print interactively.
    """

    def __init__(self, *, timeout: float = 60.0) -> None:
        self._timeout = timeout

    # ── Availability ────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True iff ``git`` is callable. Used by the installer
        as a precondition check so we can print an actionable hint
        instead of a cryptic FileNotFoundError when git isn't on PATH."""
        try:
            subprocess.run(
                ["git", "--version"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            return True
        except (
            subprocess.SubprocessError,
            FileNotFoundError,
            OSError,
        ):
            return False

    # ── Repo operations ─────────────────────────────────────────────

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        ref: str | None = None,
        shallow: bool = True,
    ) -> None:
        """Clone *url* into *dest*.

        ``ref`` may be a branch name or tag. Pinning to a SHA via
        ``--branch`` doesn't work on older git versions, so SHA pins
        are handled by clone-then-checkout in :meth:`PluginInstaller`.

        ``shallow=True`` uses ``--depth 1`` (the default) — plugins
        are small + we keep the install fast. Disable for marketplaces
        or anything where history is needed.
        """
        args = ["git", "clone"]
        if shallow:
            args += ["--depth", "1"]
        if ref:
            args += ["--branch", ref]
        args += [url, str(dest)]
        self._run(args)

    def fetch(self, repo: Path) -> None:
        """``git fetch --tags origin`` in *repo*."""
        self._run(["git", "fetch", "--tags", "origin"], cwd=repo)

    def reset_hard(self, repo: Path, ref: str) -> None:
        """``git reset --hard <ref>`` — used after fetch for updates."""
        self._run(["git", "reset", "--hard", ref], cwd=repo)

    def checkout(self, repo: Path, ref: str) -> None:
        """``git checkout <ref>`` — for SHA / tag pins after clone."""
        self._run(["git", "checkout", ref], cwd=repo)

    # ── Repo introspection ─────────────────────────────────────────

    def current_sha(self, repo: Path) -> str:
        """Return the SHA of the working-tree's HEAD."""
        result = self._run(["git", "rev-parse", "HEAD"], cwd=repo)
        return result.stdout.strip()

    def head_branch(self, repo: Path) -> str:
        """Return the default branch name reported by ``origin/HEAD``.

        Falls back to ``main`` if symbolic-ref isn't set (rare — happens
        with very-old clones or repos whose default branch was renamed
        without updating origin's symbolic ref).
        """
        try:
            result = self._run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=repo,
            )
            # Output looks like "refs/remotes/origin/main"
            return result.stdout.strip().rsplit("/", 1)[-1] or "main"
        except GitError:
            return "main"

    # ── Internal ───────────────────────────────────────────────────

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                args,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git timed out after {self._timeout}s: {' '.join(args)}") from e
        except FileNotFoundError as e:
            raise GitError(
                "'git' executable not found on PATH. Install git to use "
                "`/plugin install`, `/plugin update`, etc."
            ) from e
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"git failed (exit {result.returncode}): {stderr}")
        return result
