"""OrchestrateTools — allows agents to spawn sub-teams at runtime.

The class is a thin Agno-facing facade: three tool methods
(``spawn_agent`` / ``spawn_team`` / ``create_agent``) that
construct the appropriate :class:`SpawnRunner` and delegate.

Sibling modules own the rest:

* :class:`SpawnBudget` (``orchestrate_budget.py``) — per-session
  sub-agent counter, one instance per session vended by
  :meth:`AgentPool.spawn_budget`.
* :class:`SpawnSandbox` (``orchestrate_sandbox.py``) — one
  per-spawn worktree lifecycle. Failure mode is a typed
  :class:`SandboxSetupResult` envelope.
* :class:`SpawnRunner` + :class:`AgentSpawn` + :class:`TeamSpawn`
  (``orchestrate_spawn.py``) — template method + two concrete
  overrides for single-agent vs sub-team spawns. Reserves the
  budget, fires typed SubagentStart/Stop hooks, emits typed
  ``agent_started`` progress events.

Public re-exports (``_run_agent_streaming``, ``_run_team_streaming``)
preserve the historic import surface used by tests and callers.
"""

import sys as _sys  # used by _build_spawn_deps for late-bind attribute lookup
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Kept at module top so ``patch("ember_code.core.tools.orchestrate.Team")``
# / ``patch("...ModelRegistry")`` in tests continue to work. The
# facade itself doesn't use ``Team`` — :class:`TeamSpawn` does — but
# the patch targets were established in older iterations and tests
# still reach for them here.
from agno.team.team import Team  # noqa: F401  (patch target)
from agno.tools import Toolkit

from ember_code.core.config.models import ModelRegistry  # noqa: F401  (patch target)
from ember_code.core.tools.orchestrate_budget import SpawnBudget
from ember_code.core.tools.orchestrate_events import (
    EventAppender,
    HitlCoordinatorProtocol,
    OnProgress,
    SpawnDeps,
    SubAgentRegistry,
)
from ember_code.core.tools.orchestrate_sandbox import SpawnSandbox
from ember_code.core.tools.orchestrate_spawn import AgentSpawn, TeamSpawn
from ember_code.core.tools.orchestrate_streaming import (
    run_agent_streaming as _run_agent_streaming,
)
from ember_code.core.tools.orchestrate_streaming import (
    run_team_streaming as _run_team_streaming,
)

__all__ = [
    "OrchestrateTools",
    "_run_agent_streaming",
    "_run_team_streaming",
]

if TYPE_CHECKING:
    from ember_code.core.agents import AgentPool
    from ember_code.core.config.settings import Settings
    from ember_code.core.hooks.executor import HookExecutor


class OrchestrateTools(Toolkit):
    """Tools for agents to spawn sub-teams from the agent pool.

    Agno-facing facade — every tool method builds a
    :class:`SpawnRunner` and delegates. Collaborators
    (:class:`SpawnBudget`, :class:`SubAgentRegistry`, the
    ``on_progress`` / ``event_appender`` callbacks) live as
    instance attributes so a shallow copy for Agno's per-run
    dispatch shares the same references — a late-arriving wire
    (e.g. :meth:`TeamWiring.wire_orchestrate_progress` setting
    ``self.on_progress`` on the original) is picked up by every
    future per-run copy.
    """

    def __init__(
        self,
        pool: "AgentPool",
        settings: "Settings",
        current_depth: int = 0,
        hook_executor: "HookExecutor | None" = None,
        session_id: str = "",
        hitl_coordinator: HitlCoordinatorProtocol | None = None,
        project_dir: Path | None = None,
    ):
        super().__init__(name="ember_orchestrate")
        self.pool = pool
        self.settings = settings
        self.current_depth = current_depth
        self.max_depth = settings.orchestration.max_nesting_depth
        self._hook_executor = hook_executor
        self._session_id = session_id
        # Required for ``isolation="worktree"`` spawns — the worktree
        # is forked from this repo. ``None`` disables the isolation
        # feature (spawn_agent returns an error if the agent
        # requests it without a project_dir wired in).
        self._project_dir = project_dir
        # Per-session budget owned by the pool (one instance per
        # session_id — see :meth:`AgentPool.spawn_budget`).
        self._budget: SpawnBudget = pool.spawn_budget(session_id)
        # Progress callback wired by
        # :meth:`TeamWiring.wire_orchestrate_progress` at session
        # bootstrap. When set, sub-agent lifecycle events get
        # forwarded here so the backend can surface them as
        # ``orchestrate_event`` push notifications.
        self.on_progress: OnProgress | None = None
        # Session event-log appender — see
        # :meth:`Session.append_event`. Wired by
        # ``TeamWiring.wire_orchestrate_event_appender`` on session
        # bootstrap. Signature: async (event_type, payload, run_id)
        # -> None.
        self.event_appender: EventAppender | None = None
        # Cancellation registry for in-flight sub-agent run_ids.
        # Agno's cooperative cancel (``Agent.cancel_run(run_id)``)
        # flags exactly one run_id — the top-level team's cancel
        # does NOT propagate to sub-agents that Agno assigned
        # distinct run_ids (visualizer, editor, every specialist).
        # ``BackendServer.cancel_run`` iterates this registry and
        # cancels every entry so a stuck sub-agent (e.g. a
        # visualizer retrying a truncated tool call in a loop)
        # actually stops when the user hits ESC.
        self.subagent_registry = SubAgentRegistry()
        # When set, sub-agent pauses get pushed here so the backend
        # can surface them as ordinary HITL requests. Without it,
        # sub-agent tool calls that need confirmation will silently
        # return empty results — see core/sub_agent_hitl.py.
        self._hitl_coordinator = hitl_coordinator
        self.register(self.spawn_agent)
        self.register(self.spawn_team)
        if settings.orchestration.generate_ephemeral:
            self.register(self.create_agent)

    # ── Legacy shims kept for tests that reach into the class ────
    #
    # ``tests/test_orchestrate_worktree.py`` still calls the
    # underscore-prefixed staticmethod and instance methods on
    # OrchestrateTools. The real logic moved to
    # :class:`SpawnSandbox`; these are thin delegations kept only
    # to avoid a same-PR test rewrite. New callers should use
    # :class:`SpawnSandbox` directly.

    @staticmethod
    def _rebind_tool_base_dirs(agent: Any, new_base: Path) -> dict[Any, Any]:
        """Delegates to :meth:`SpawnSandbox.rebind_tool_base_dirs`."""
        return SpawnSandbox.rebind_tool_base_dirs(agent, new_base)

    def _create_isolated_worktree(self, agent_name: str):
        """Delegates to :meth:`SpawnSandbox.create` in worktree mode.

        Preserves the legacy ``(manager, info_or_error)`` tuple
        return shape used by the test suite. Successful setup
        yields ``(manager, info)``; failure yields
        ``(None, error_string)``.
        """
        result = SpawnSandbox.create(
            project_dir=self._project_dir,
            session_id=self._session_id,
            agent=None,  # tests bypass rebind by passing agent=None
            agent_name=agent_name,
            isolation="worktree",
            task="",
        )
        if result.error is not None:
            return None, result.error
        sandbox = result.sandbox
        return sandbox.manager, sandbox.info

    # ── Dependency wiring ────────────────────────────────────────

    def _build_spawn_deps(self) -> SpawnDeps:
        """Bundle the four late-bound module attributes into a
        :class:`SpawnDeps`.

        Read from ``sys.modules[__name__]`` at call time (not from the
        local ``from ... import`` bindings above) so tests that patch
        ``ember_code.core.tools.orchestrate._run_agent_streaming`` /
        ``.Team`` / ``.ModelRegistry`` see their patches take effect
        on every spawn. This is the ONE permitted late-bind site
        (Rule 2 carve-out for a genuine circular-import break:
        ``orchestrate`` imports ``orchestrate_spawn``, so the reverse
        static import isn't allowed).
        """
        mod = _sys.modules[__name__]
        return SpawnDeps(
            agent_streamer=mod._run_agent_streaming,
            team_streamer=mod._run_team_streaming,
            team_factory=mod.Team,
            model_registry_factory=mod.ModelRegistry,
        )

    # ── Tool methods (Agno-facing) ───────────────────────────────

    async def spawn_agent(
        self,
        task: str,
        agent_name: str,
        isolation: str = "",
    ) -> str:
        """Run a single agent from the pool on a subtask.

        Args:
            task: The subtask description for the agent.
            agent_name: Name of the agent to spawn (from the pool).
            isolation: Optional isolation mode. Currently the only
                non-empty value is ``"worktree"`` — creates a
                fresh git worktree branched off the session's
                project, runs the agent with its file/shell tools
                rebased to that worktree, then either cleans up
                (no changes) or preserves the worktree (changes
                remain on the new branch for the caller to merge
                or discard). Tools without a ``base_dir``
                attribute (most MCP clients) still see the
                original project dir.

        Returns:
            The agent's response with activity log. When the
            spawn was isolated, a ``Worktree:`` footer reports
            the branch + path so the caller knows where the
            changes landed.
        """
        runner = AgentSpawn(
            agent_name=agent_name,
            task=task,
            isolation=isolation,
            pool=self.pool,
            settings=self.settings,
            session_id=self._session_id,
            hook_executor=self._hook_executor,
            on_progress=self.on_progress,
            event_appender=self.event_appender,
            subagent_registry=self.subagent_registry,
            hitl_coordinator=self._hitl_coordinator,
            budget=self._budget,
            current_depth=self.current_depth,
            max_depth=self.max_depth,
            project_dir=self._project_dir,
            deps=self._build_spawn_deps(),
        )
        return await runner.run()

    async def spawn_team(self, task: str, agent_names: str, mode: str = "coordinate") -> str:
        """Create and run a sub-team for a specific subtask.

        Args:
            task: The subtask description.
            agent_names: Comma-separated agent names from the pool.
            mode: Team mode: "coordinate", "route", "broadcast", or "tasks".

        Returns:
            The team's response with activity log.
        """
        names = [n.strip() for n in agent_names.split(",") if n.strip()]
        # Single-name teams collapse to a plain spawn — preserved
        # behaviour from the pre-refactor code.
        if len(names) == 1:
            return await self.spawn_agent(task, names[0])
        runner = TeamSpawn(
            names=names,
            task=task,
            mode=mode,
            pool=self.pool,
            settings=self.settings,
            session_id=self._session_id,
            hook_executor=self._hook_executor,
            on_progress=self.on_progress,
            event_appender=self.event_appender,
            subagent_registry=self.subagent_registry,
            hitl_coordinator=self._hitl_coordinator,
            budget=self._budget,
            current_depth=self.current_depth,
            max_depth=self.max_depth,
            project_dir=self._project_dir,
            deps=self._build_spawn_deps(),
        )
        return await runner.run()

    def create_agent(
        self,
        name: str,
        description: str,
        system_prompt: str,
        tools: str = "Read,Write,Edit,Bash,Grep,Glob",
    ) -> str:
        """Create a new ephemeral agent with a custom system prompt.

        Args:
            name: Short snake_case name for the agent.
            description: One-line description of what the agent does.
            system_prompt: Full system prompt defining the agent's behavior.
            tools: Comma-separated tool names (e.g. "Read,Write,Edit,Bash,Grep,Glob").
                Valid: Read, Write, Edit, Bash, Grep, Glob, LS, WebSearch, WebFetch,
                Python, Schedule, NotebookEdit.

        Returns:
            Confirmation message with the agent name.
        """
        # ``tools: str`` (CSV) is an intentional exception to the
        # audit's "list[str] over CSV string" preference: Agno's
        # tool-argument marshalling emits strings for tool arguments,
        # not lists, so a ``list[str]`` here breaks the tool contract.
        tool_list = [t.strip() for t in tools.split(",") if t.strip()]
        try:
            self.pool.register_ephemeral(
                name=name,
                description=description,
                system_prompt=system_prompt,
                tools=tool_list,
            )
            return (
                f"Created ephemeral agent '{name}': {description}. "
                f"Use spawn_agent(task, '{name}') to delegate."
            )
        except (ValueError, RuntimeError) as e:
            return f"Error creating agent: {e}"
