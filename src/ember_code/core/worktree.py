"""Git worktree lifecycle manager for isolated parallel sessions.

Two collaborating classes live here:

* :class:`WorktreeRoot` — owns the shared ``~/.ember/worktrees``
  root directory and hosts multi-worktree operations
  (:meth:`prune_stale`). Instances are cheap; construct one per
  subsystem or share the module-level default. Tests inject a
  ``tmpdir`` via the ``root=`` kwarg.
* :class:`WorktreeManager` — owns one single worktree's lifecycle
  (create → inspect → cleanup). Composed with a
  :class:`WorktreeRoot` so it never touches ``Path.home()``
  directly. :meth:`cleanup` returns a typed
  :class:`WorktreeCleanupResult`; :meth:`create_result` returns
  a typed :class:`WorktreeCreateResult`. The legacy
  :meth:`create` still raises on failure — a Pattern-3 migration
  step; new code should call :meth:`create_result`.

Four Pydantic models — :class:`WorktreeInfo`,
:class:`WorktreeCreateResult`, :class:`WorktreeCleanupResult`,
:class:`StalePruneResult` — are co-located here because the
model count is small and the subsystem is cohesive; if this file
grows past ~350 LoC (e.g. adding merge / rebase orchestration)
promote to a subpackage with a ``schemas.py``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Stateless leaf helper: run a git command capturing text output.

    Centralises the ``subprocess.run(["git", ...], capture_output=True,
    text=True)`` invocation shared by :class:`WorktreeRoot` and
    :class:`WorktreeManager` so a future signature change (e.g. a
    timeout) touches one place. Module-scoped because it takes only
    primitives — Rule 6's stateless-leaf exception applies.
    """
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


# ─── Models ─────────────────────────────────────────────────────


class WorktreeInfo(BaseModel):
    """Information about an active git worktree."""

    worktree_path: Path
    branch_name: str
    original_dir: Path


class WorktreeCreateResult(BaseModel):
    """Outcome of :meth:`WorktreeManager.create`.

    ``ok=True`` iff the worktree was created; ``info`` is then
    populated. Failure modes are enumerated via the ``error``
    discriminator so callers can branch on them without parsing
    strings.
    """

    ok: bool
    info: WorktreeInfo | None = None
    error: Literal[
        "",
        "not_a_git_repo",
        "path_exists",
        "git_add_failed",
    ] = ""
    message: str = ""


class WorktreeCleanupResult(BaseModel):
    """Outcome of :meth:`WorktreeManager.cleanup`.

    ``status`` values:

    * ``"cleaned"`` — worktree removed and branch deleted.
    * ``"preserved_dirty"`` — worktree kept because it has
      uncommitted / untracked changes.
    * ``"noop"`` — manager owns no worktree (already cleaned or
      never created).
    * ``"git_remove_failed"`` — ``git worktree remove`` returned
      non-zero.
    * ``"branch_delete_failed"`` — worktree was removed but
      ``git branch -d`` failed.

    ``.ok`` is a convenience alias for ``status == "cleaned"``.
    ``__bool__`` is truthy when reaped or a no-op, so legacy
    ``if wm.cleanup(): ...`` sites keep working during migration.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: Literal[
        "cleaned",
        "preserved_dirty",
        "noop",
        "git_remove_failed",
        "branch_delete_failed",
    ]
    info: WorktreeInfo | None = None
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """True iff the worktree was reaped (branch + dir removed)."""
        return self.status == "cleaned"


class StalePruneResult(BaseModel):
    """Outcome of :meth:`WorktreeRoot.prune_stale`.

    ``pruned_by_git`` reflects the ``git worktree prune`` return
    code. Non-fatal stderr from git (e.g. locked worktrees) is
    kept as ``warnings`` so callers can surface it without
    treating it as an error.
    """

    pruned_by_git: bool
    warnings: list[str] = Field(default_factory=list)
    empty_dirs_removed: list[str] = Field(default_factory=list)


# ─── Classes ────────────────────────────────────────────────────


class WorktreeRoot:
    """Owns the shared ``~/.ember/worktrees`` directory.

    Instances are cheap; the default constructor uses
    ``~/.ember/worktrees`` but tests inject a ``tmpdir`` via
    ``WorktreeRoot(root=tmpdir)``. Multi-worktree operations
    (:meth:`prune_stale`) live here because they span every
    worktree under the root, not just one manager's.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else (Path.home() / ".ember" / "worktrees")

    def ensure_exists(self) -> None:
        """Create the root directory tree if missing (idempotent)."""
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, branch_name: str) -> Path:
        """Return the worktree directory path for ``branch_name``."""
        return self.root / branch_name

    def manager_for(self, repo_dir: Path) -> WorktreeManager:
        """Return a :class:`WorktreeManager` bound to this root."""
        return WorktreeManager(repo_dir, root=self)

    def prune_stale(self, repo_dir: Path) -> StalePruneResult:
        """Remove stale worktrees that reference missing directories.

        Runs ``git worktree prune`` in ``repo_dir`` and cleans up
        empty leftover directories under the root. Non-fatal git
        stderr is surfaced as ``warnings`` so callers can log
        without confusing them for errors.
        """
        if not self.root.exists():
            return StalePruneResult(pruned_by_git=False)

        prune = _run_git(
            ["git", "worktree", "prune"],
            cwd=repo_dir,
        )
        warnings: list[str] = []
        stderr = (prune.stderr or "").strip()
        if stderr:
            warnings.append(stderr)

        empty_dirs_removed: list[str] = []
        for child in self.root.iterdir():
            if child.is_dir() and not any(child.iterdir()):
                shutil.rmtree(child, ignore_errors=True)
                empty_dirs_removed.append(child.name)

        return StalePruneResult(
            pruned_by_git=prune.returncode == 0,
            warnings=warnings,
            empty_dirs_removed=empty_dirs_removed,
        )


class WorktreeManager:
    """Create, inspect, and clean up a single git worktree.

    Worktrees are created under ``WorktreeRoot.root`` (default
    ``~/.ember/worktrees``) so they don't clutter the project
    directory.

    Construction is thin: the constructor validates the git repo
    (raises ``RuntimeError`` on non-repo — preserved for
    backward compatibility with legacy call sites). Prefer
    :meth:`WorktreeRoot.manager_for` when you need to inject a
    custom root; the direct ``WorktreeManager(repo_dir)`` path
    stays available for the two existing call sites in
    ``cli.py`` and ``orchestrate_sandbox.py``.
    """

    def __init__(self, repo_dir: Path, root: WorktreeRoot | None = None) -> None:
        self.repo_dir = repo_dir.resolve()
        self.root = root if root is not None else WorktreeRoot()
        self._info: WorktreeInfo | None = None
        self._validate_git_repo()

    # ─── Static helpers ─────────────────────────────────────────

    @staticmethod
    def _short_suffix() -> str:
        """Return an 8-char random suffix for auto-generated branch names."""
        return str(uuid.uuid4())[:8]

    # ─── Lifecycle ──────────────────────────────────────────────

    def _validate_git_repo(self) -> None:
        """Raise ``RuntimeError`` if ``repo_dir`` is not a git worktree.

        Kept as a raise (rather than a Result) because it runs at
        construction time and the two production call sites
        (``cli.py``, ``orchestrate_sandbox.py``) already handle
        the ``RuntimeError`` — flipping it would be a wider
        surface change than the audit scope.
        """
        result = _run_git(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=self.repo_dir,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Not a git repository: {self.repo_dir}")

    def create(
        self,
        branch_name: str | None = None,
        session_id: str | None = None,
    ) -> WorktreeInfo:
        """Create a new git worktree on a fresh branch.

        Args:
            branch_name: Explicit branch name. Auto-generated if
                ``None``.
            session_id: Used as the auto-generated branch suffix
                when ``branch_name`` is ``None``.

        Returns:
            :class:`WorktreeInfo` on success.

        Raises:
            RuntimeError: If the target path already exists or
                ``git worktree add`` fails. Kept as a raise for
                backward compatibility — see
                :meth:`create_result` for the Result-shaped
                variant.
        """
        result = self.create_result(branch_name=branch_name, session_id=session_id)
        if not result.ok or result.info is None:
            raise RuntimeError(result.message)
        return result.info

    def create_result(
        self,
        branch_name: str | None = None,
        session_id: str | None = None,
    ) -> WorktreeCreateResult:
        """Result-shaped variant of :meth:`create`.

        Returns a :class:`WorktreeCreateResult` instead of
        raising, so callers can enumerate the failure modes
        (``path_exists``, ``git_add_failed``) without parsing
        exception messages. Prefer this in new code; the
        raising :meth:`create` remains for the two legacy call
        sites in ``cli.py`` and ``orchestrate_sandbox.py``.
        """
        if branch_name is None:
            suffix = session_id or self._short_suffix()
            branch_name = f"ember-worktree-{suffix}"

        self.root.ensure_exists()
        worktree_path = self.root.path_for(branch_name)

        if worktree_path.exists():
            return WorktreeCreateResult(
                ok=False,
                error="path_exists",
                message=(
                    f"Worktree path already exists: {worktree_path}. "
                    f"Run 'git worktree remove {worktree_path}' to clean up."
                ),
            )

        proc = _run_git(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
            cwd=self.repo_dir,
        )
        if proc.returncode != 0:
            return WorktreeCreateResult(
                ok=False,
                error="git_add_failed",
                message=f"Failed to create worktree: {proc.stderr.strip()}",
            )

        self._info = WorktreeInfo(
            worktree_path=worktree_path,
            branch_name=branch_name,
            original_dir=self.repo_dir,
        )
        logger.info("Created worktree at %s (branch: %s)", worktree_path, branch_name)
        return WorktreeCreateResult(ok=True, info=self._info)

    @property
    def info(self) -> WorktreeInfo | None:
        return self._info

    def has_changes(self) -> bool:
        """Check if the worktree has uncommitted or staged changes."""
        if self._info is None:
            return False
        result = _run_git(
            ["git", "status", "--porcelain"],
            cwd=self._info.worktree_path,
        )
        return bool(result.stdout.strip())

    def cleanup(self) -> WorktreeCleanupResult:
        """Remove the worktree and its branch if no changes were made.

        Returns:
            :class:`WorktreeCleanupResult` — ``status`` enumerates
            reaped / preserved / failed outcomes. ``__bool__`` is
            preserved so legacy ``if wm.cleanup(): ...`` call sites
            keep working (truthy when reaped or no-op), but new
            code should check ``.status`` or ``.ok`` explicitly.
        """
        if self._info is None:
            return WorktreeCleanupResult(status="noop")

        if self.has_changes():
            logger.info(
                "Worktree has changes — preserving at %s (branch: %s)",
                self._info.worktree_path,
                self._info.branch_name,
            )
            return WorktreeCleanupResult(status="preserved_dirty", info=self._info)

        wt_path = self._info.worktree_path
        branch = self._info.branch_name
        original_dir = self._info.original_dir

        # Remove worktree — surface non-zero as a status so
        # callers no longer see cleanup-failure as success.
        remove_proc = _run_git(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=original_dir,
        )
        if remove_proc.returncode != 0:
            return WorktreeCleanupResult(
                status="git_remove_failed",
                info=self._info,
                stderr=remove_proc.stderr.strip(),
            )

        # Delete the branch (safe — no changes).
        branch_proc = _run_git(
            ["git", "branch", "-d", branch],
            cwd=original_dir,
        )
        if branch_proc.returncode != 0:
            # Worktree is gone but branch remains — surface it so
            # the caller can decide whether to force-delete.
            self._info = None
            return WorktreeCleanupResult(
                status="branch_delete_failed",
                info=None,
                stderr=branch_proc.stderr.strip(),
            )

        logger.info("Cleaned up worktree at %s (branch: %s)", wt_path, branch)
        self._info = None
        return WorktreeCleanupResult(status="cleaned")

    def report_cleanup(self, echo: Callable[..., None]) -> None:
        """Clean up the worktree and echo a human-readable summary.

        ``echo`` is injected (typically :func:`click.echo`) so this
        method stays UI-agnostic — no ``click`` import in this
        module. Absorbed from the free ``_worktree_cleanup(wm)``
        helper that used to live in ``cli.py``: taking the manager
        as the first arg made it a method-in-disguise, so promoting
        it here removes the state-first-arg smell without leaking a
        UI dependency.

        Guarded on ``self.info is None`` FIRST so this is a safe
        no-op when no worktree was created (matches the old
        ``_worktree_cleanup`` short-circuit and keeps
        ``cleanup.assert_not_called()`` tests green).
        """
        if self.info is None:
            return
        info = self.info
        result = self.cleanup()
        if result.status == "cleaned":
            echo("Worktree cleaned up (no changes).")
        elif result.status == "preserved_dirty":
            echo("\nWorktree preserved (has changes):")
            echo(f"  Path:   {info.worktree_path}")
            echo(f"  Branch: {info.branch_name}")
            echo(f"\nTo merge: git merge {info.branch_name}")
            echo(f"To remove: git worktree remove {info.worktree_path}")
        else:
            # ``git_remove_failed`` / ``branch_delete_failed`` —
            # surface the stderr so cleanup failures no longer
            # masquerade as success (audit fix: silent-cleanup path).
            echo(
                f"\nWorktree cleanup failed ({result.status}): {result.stderr}",
                err=True,
            )
            echo(f"  Path:   {info.worktree_path}")
            echo(f"  Branch: {info.branch_name}")


__all__ = [
    "WorktreeInfo",
    "WorktreeCreateResult",
    "WorktreeCleanupResult",
    "StalePruneResult",
    "WorktreeRoot",
    "WorktreeManager",
]
