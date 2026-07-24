"""Per-spawn worktree sandbox for :mod:`orchestrate`.

The class collects everything the old free-function chain
(``_create_isolated_worktree`` → ``_rebind_tool_base_dirs`` →
``finalize``) needed to share across a single spawn:
the worktree manager, the worktree info, the map of original
tool ``base_dir`` values, and the augmented task string. One
:class:`SpawnSandbox` instance is constructed per
``spawn_agent`` call and passed to the :class:`SpawnRunner` so
the exception-path cleanup is a single ``sandbox.finalize()``
call in a ``finally`` block.

Isolation modes today:

* ``""`` — no worktree; :meth:`create` returns an empty sandbox
  and :meth:`finalize` is a no-op (the tool ``base_dir`` map is
  empty, ``manager`` / ``info`` are ``None``).
* ``"worktree"`` — Claude-Code parity mode; ``spawn_agent``
  forks a fresh git worktree branched off the session's project
  and rebases every tool's ``base_dir`` onto it. Tools without
  a ``base_dir`` attribute (MCP clients, etc.) still see the
  project root — the augmented task string tells the model to
  operate within the sandbox path so it uses absolute paths.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.core.tools.orchestrate_events import SandboxSetupResult
from ember_code.core.worktree import WorktreeManager

if TYPE_CHECKING:
    from ember_code.core.worktree import WorktreeInfo

logger = logging.getLogger(__name__)


class SpawnSandbox:
    """Owns one spawn's worktree lifecycle.

    Constructed via the :meth:`create` classmethod so failure to
    fork a worktree is expressed as a
    :class:`SandboxSetupResult` (audit Pattern 3 — Result over
    raise-catch for expected failures). Instance methods
    :meth:`rebind_tool_base_dirs` and :meth:`finalize` cover the
    other two lifecycle points; call sites in
    :class:`SpawnRunner` never touch the worktree manager
    directly.
    """

    __slots__ = ("manager", "info", "original_base_dirs", "task")

    def __init__(
        self,
        *,
        manager: WorktreeManager | None,
        info: WorktreeInfo | None,
        original_base_dirs: dict[Any, Any],
        task: str,
    ) -> None:
        self.manager = manager
        self.info = info
        self.original_base_dirs = original_base_dirs
        self.task = task

    @classmethod
    def create(
        cls,
        *,
        project_dir: Path | None,
        session_id: str,
        agent: Any,
        agent_name: str,
        isolation: str,
        task: str,
    ) -> SandboxSetupResult:
        """Build a sandbox for one spawn.

        ``isolation == ""`` returns an empty (no-op) sandbox — the
        caller can still call :meth:`finalize` unconditionally and
        get back an empty footer.

        ``isolation == "worktree"`` creates a fresh git worktree
        branched off ``project_dir``, rebases every ``base_dir``-
        bearing tool on ``agent`` to that worktree, and returns
        the populated sandbox plus a task-preamble instructing the
        model to operate within it.

        Failures (missing project dir, non-git repo, path
        collision) come back as
        :class:`SandboxSetupResult` with a populated ``error``.
        """
        if isolation != "worktree":
            return SandboxSetupResult(
                sandbox=cls(
                    manager=None,
                    info=None,
                    original_base_dirs={},
                    task=task,
                ),
                task=task,
                error=None,
            )

        if project_dir is None:
            return SandboxSetupResult(
                sandbox=None,
                error="Error: isolation=worktree requires a project directory.",
            )
        try:
            manager = WorktreeManager(project_dir)
        except RuntimeError as exc:
            return SandboxSetupResult(
                sandbox=None,
                error=f"Error: cannot create worktree — {exc}",
            )
        # Short, stable suffix so multiple isolated spawns
        # within one session don't collide on the worktree path.
        wt_suffix = f"{session_id[:8] or 'sess'}-{agent_name}-{uuid.uuid4().hex[:6]}"
        create_result = manager.create_result(session_id=wt_suffix)
        if not create_result.ok or create_result.info is None:
            return SandboxSetupResult(
                sandbox=None,
                error=f"Error: worktree create failed — {create_result.message}",
            )
        info = create_result.info

        # ``agent is None`` is a legacy shim path
        # (:meth:`OrchestrateTools._create_isolated_worktree` in
        # test scaffolds) — skip the rebind and hand back an empty
        # ``original_base_dirs`` map so :meth:`finalize` stays a
        # no-op on the tool side.
        if agent is None:
            original_base_dirs: dict[Any, Any] = {}
        else:
            original_base_dirs = cls.rebind_tool_base_dirs(agent, info.worktree_path)
        worktree_task = (
            f"You are running in an isolated git worktree at "
            f"{info.worktree_path} (branch: "
            f"{info.branch_name}). Treat that path as "
            f"your working directory — operate within it.\n\n"
            f"{task}"
        )
        return SandboxSetupResult(
            sandbox=cls(
                manager=manager,
                info=info,
                original_base_dirs=original_base_dirs,
                task=worktree_task,
            ),
            task=worktree_task,
            error=None,
        )

    @staticmethod
    def rebind_tool_base_dirs(agent: Any, new_base: Path) -> dict[Any, Any]:
        """Best-effort: point every toolkit on ``agent`` at
        ``new_base``. Returns ``{toolkit: original_base_dir}`` so
        callers can restore after the spawn completes.

        Shallow-copies each toolkit so the rebind is local to
        THIS spawn — the pool's shared agent instance keeps its
        original tool refs untouched. Toolkits without a
        ``base_dir`` attribute (MCP clients, the orchestrate
        toolkit itself, etc.) are left alone.
        """
        if not hasattr(agent, "tools") or agent.tools is None:
            return {}
        try:
            agent.tools = [copy.copy(t) for t in agent.tools]
        except Exception:
            # Some toolkits can't be shallow-copied (rare). Bail
            # without raising — partial isolation beats hard fail.
            return {}
        originals: dict[Any, Any] = {}
        for tool in agent.tools:
            if hasattr(tool, "base_dir"):
                originals[tool] = tool.base_dir
                with contextlib.suppress(Exception):
                    tool.base_dir = new_base
        return originals

    def finalize(self) -> str:
        """Restore tool ``base_dir`` rebinds and clean up the
        worktree.

        Returns a footer string for the spawn response so the
        parent agent knows whether the worktree was reaped or
        preserved. Idempotent and exception-safe — designed to
        run in ``finally`` blocks.
        """
        # Restore tool base_dirs first — the worktree dir may
        # disappear in the cleanup step below, and a stray
        # reference to it after that would point at a missing path.
        for tool, original in self.original_base_dirs.items():
            with contextlib.suppress(Exception):
                tool.base_dir = original
        if self.manager is None or self.info is None:
            return ""
        try:
            result = self.manager.cleanup()
        except Exception as exc:
            logger.warning("worktree cleanup failed: %s", exc)
            return (
                f"\n\nWorktree: {self.info.worktree_path} (branch: "
                f"{self.info.branch_name}) — cleanup failed: {exc}"
            )
        if result.status == "cleaned":
            return f"\n\nWorktree {self.info.branch_name} (clean) — reaped."
        if result.status == "preserved_dirty":
            return (
                f"\n\nWorktree preserved: {self.info.worktree_path} "
                f"(branch: {self.info.branch_name}) — has uncommitted changes.\n"
                f"To merge: git merge {self.info.branch_name}\n"
                f"To remove: git worktree remove {self.info.worktree_path}"
            )
        # git_remove_failed / branch_delete_failed — surface the
        # stderr so cleanup failures no longer masquerade as
        # success (audit fix: silent-cleanup path).
        return (
            f"\n\nWorktree: {self.info.worktree_path} (branch: "
            f"{self.info.branch_name}) — cleanup {result.status}: "
            f"{result.stderr}"
        )


__all__ = ["SpawnSandbox"]
