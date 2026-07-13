"""OrchestrateTools — allows agents to spawn sub-teams at runtime."""

import asyncio
import contextlib
import copy
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.run import agent as agent_events
from agno.run import team as team_events
from agno.team.team import Team
from agno.tools import Toolkit

from ember_code.core.config.models import ModelRegistry
from ember_code.core.tools.subagent_stream import SubAgentStreamState
from ember_code.core.worktree import WorktreeManager
from ember_code.core.tools.orchestrate_helpers import (
    PREVIEW_LINE_MAX,
    PREVIEW_WINDOW,
    VisualizationDeltaEvent,
    _build_preview,
    _extract_spec_from_partial_args,
    _finalize_worktree,
    _format_args,
    _format_spawn_result,
    _format_team_result,
    _preview,
)
from ember_code.core.tools.orchestrate_streaming import (
    run_agent_streaming as _run_agent_streaming,
    run_team_streaming as _run_team_streaming,
)

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.hooks.executor import HookExecutor
    from ember_code.core.pool import AgentPool
    from ember_code.core.worktree import WorktreeInfo

_VALID_ISOLATION_MODES: frozenset[str] = frozenset({"", "worktree"})

logger = logging.getLogger(__name__)

_agent_counter_lock = threading.Lock()
_agent_counters: dict[str, int] = {}


class OrchestrateTools(Toolkit):
    """Tools for agents to spawn sub-teams from the agent pool."""

    # Class-level slot. ``wire_orchestrate_progress`` sets this at
    # the CLASS level (not per-instance) because Agno copies the
    # toolkit per-run — an instance-level attribute on the wired
    # original wouldn't reach the copy the running agent invokes.
    # Every instance reads it via Python's normal attribute lookup
    # so long as no instance-level shadow is set in ``__init__``.
    _on_progress: Any = None

    # Class-level slot for the session's event-log appender. Same
    # per-run-copy reasoning as ``_on_progress``. Wired by
    # ``wire_orchestrate_event_log`` on session bootstrap.
    # Signature: ``async (event_type: str, payload: dict, run_id: str) -> None``.
    _append_event: Any = None

    # Class-level registry of currently-in-flight sub-agent run_ids.
    # Agno's cooperative cancel (``Agent.cancel_run(run_id)``) flags
    # exactly one run_id — the top-level team's cancel does NOT
    # propagate to sub-agents that Agno assigned distinct run_ids
    # (visualizer, editor, every specialist). ``BackendServer.cancel_run``
    # reads this set and cancels every entry so a stuck sub-agent
    # (e.g. a visualizer retrying a truncated tool call in a loop)
    # actually stops when the user hits ESC.
    #
    # Adds happen in ``_run_agent_streaming`` /
    # ``_run_team_streaming`` on the first event carrying a run_id;
    # removes happen in the outer ``try/finally`` so a mid-stream
    # exception still cleans up.
    _active_subagent_runs: set[str] = set()

    def __init__(
        self,
        pool: "AgentPool",
        settings: "Settings",
        current_depth: int = 0,
        hook_executor: "HookExecutor | None" = None,
        session_id: str = "",
        hitl_coordinator: Any = None,
        project_dir: Path | None = None,
    ):
        super().__init__(name="ember_orchestrate")
        self.pool = pool
        self.settings = settings
        self.current_depth = current_depth
        self.max_depth = settings.orchestration.max_nesting_depth
        self._hook_executor = hook_executor
        self._session_id = session_id
        self._max_agents = settings.orchestration.max_total_agents
        # Required for ``isolation="worktree"`` spawns — the
        # worktree is forked from this repo. ``None`` disables
        # the isolation feature (spawn_agent returns an error if
        # the agent requests it without a project_dir wired in).
        self._project_dir = project_dir
        # NOTE: ``_on_progress`` is a CLASS attribute (see above) —
        # do NOT initialize it here or it'll shadow the class-level
        # slot with an instance-level ``None``, breaking the wire.
        # When set, sub-agent pauses get pushed here so the backend can
        # surface them as ordinary HITL requests. Without it, sub-agent
        # tool calls that need confirmation will silently return empty
        # results — see core/sub_agent_hitl.py.
        self._hitl_coordinator = hitl_coordinator
        self.register(self.spawn_agent)
        self.register(self.spawn_team)
        if settings.orchestration.generate_ephemeral:
            self.register(self.create_agent)

    def _check_agent_limit(self, count: int = 1) -> str | None:
        with _agent_counter_lock:
            current = _agent_counters.get(self._session_id, 0)
            if current + count > self._max_agents:
                return f"Error: Maximum total agents ({self._max_agents}) reached."
            _agent_counters[self._session_id] = current + count
            return None

    async def _fire_hook(self, event: str, extra: dict[str, Any] | None = None) -> None:
        if not self._hook_executor:
            return
        payload = {"session_id": self._session_id}
        if extra:
            payload.update(extra)
        with contextlib.suppress(Exception):
            await self._hook_executor.execute(event=event, payload=payload)

    def _create_isolated_worktree(self, agent_name: str):
        """Create a fresh worktree for an isolated spawn.

        Returns ``(WorktreeManager, WorktreeInfo)`` on success, or
        ``(None, error_string)`` if creation failed. Failures are
        surfaced as ``Error: ...`` strings so the agent sees the
        reason (not a repo, worktree path collision, etc.) and
        can fall back to non-isolated spawning.

        Mirrors Claude Code's ``isolation: "worktree"`` workflow
        flag — each subagent gets its own working tree so file
        mutations across parallel spawns don't conflict.
        """
        if self._project_dir is None:
            return None, "Error: isolation=worktree requires a project directory."
        try:
            manager = WorktreeManager(self._project_dir)
        except RuntimeError as exc:
            return None, f"Error: cannot create worktree — {exc}"
        try:
            # Short, stable suffix encoded into the branch name so
            # multiple isolated spawns within one session don't
            # collide on the worktree path.
            wt_suffix = f"{self._session_id[:8] or 'sess'}-{agent_name}-{uuid.uuid4().hex[:6]}"
            info = manager.create(session_id=wt_suffix)
        except RuntimeError as exc:
            return None, f"Error: worktree create failed — {exc}"
        return manager, info

    @staticmethod
    def _rebind_tool_base_dirs(agent: Any, new_base: Path) -> dict:
        """Best-effort: point every toolkit on ``agent`` at
        ``new_base``. Returns ``{toolkit: original_base_dir}`` so
        callers can restore after the spawn completes.

        Shallow-copies each toolkit so the rebind is local to
        THIS spawn — the pool's shared agent instance keeps its
        original tool refs untouched. Toolkits without a
        ``base_dir`` attribute (MCP clients, the orchestrate
        toolkit itself, etc.) are left alone; documented caveat
        in ``spawn_agent``."""
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

    def _build_sub_team(
        self, names: list[str], mode: str
    ) -> str | tuple[Team, str]:
        """Assemble a sub-:class:`Team` for a spawn_team call.

        Returns either an error string (unknown member name) or
        ``(team, resolved_mode)`` where ``resolved_mode`` is the
        caller's mode after normalisation (unknown → "coordinate").

        Members get per-spawn shallow copies — see ``spawn_agent``
        for the rationale. Sub-team members race on shared per-run
        state without the copy.

        Shares the session's DB with the pool so Agno's
        ``team.acontinue_run(run_id, session_id)`` (called when a
        member pauses for HITL during broadcast/coordinate mode)
        can resolve the run — without it, broadcast_* /
        single_specialist_* cases hit "No runs found for run ID
        …" and turned into 60s case-timeouts in the eval. Same
        fix as ``pool.py`` for specialist agents.
        """
        members = []
        for name in names:
            try:
                members.append(copy.copy(self.pool.get(name)))
            except KeyError as e:
                return str(e)

        valid_modes = ("route", "coordinate", "broadcast", "tasks")
        if mode not in valid_modes:
            mode = "coordinate"

        team_model = ModelRegistry(self.settings).get_model()
        team_kwargs: dict[str, Any] = {
            "name": f"sub-team-depth-{self.current_depth + 1}",
            "mode": mode,
            "model": team_model,
            "members": members,
            "markdown": True,
        }
        pool_db = getattr(self.pool, "_db", None)
        if pool_db is not None:
            team_kwargs["db"] = pool_db
        if mode == "tasks":
            team_kwargs["max_iterations"] = self.settings.orchestration.max_task_iterations

        return Team(**team_kwargs), mode

    def _setup_isolation(
        self,
        agent: Any,
        agent_name: str,
        isolation: str,
        task: str,
    ) -> str | tuple[Any, "WorktreeInfo | None", dict[Any, Any], str]:
        """Set up per-spawn worktree isolation when requested.

        Returns either:
        * an error string (worktree creation failed — caller
          surfaces it verbatim to the agent), or
        * ``(worktree_manager, worktree_info, original_base_dirs,
          worktree_task)`` — always populated even when
          ``isolation`` is empty (in which case ``worktree_manager``
          / ``worktree_info`` are None, ``original_base_dirs`` is
          empty, and ``worktree_task`` == ``task``).

        The worktree_task gets a preamble telling the model where
        its sandbox is. Many tools respect ``base_dir``; the few
        that don't (custom MCP, etc.) still see the project root,
        so the explicit instruction nudges the agent to pass
        absolute paths within the worktree.
        """
        if isolation != "worktree":
            return None, None, {}, task
        worktree_manager, info_or_err = self._create_isolated_worktree(agent_name)
        if worktree_manager is None:
            return info_or_err
        worktree_info = info_or_err
        original_base_dirs = self._rebind_tool_base_dirs(agent, worktree_info.worktree_path)
        worktree_task = (
            f"You are running in an isolated git worktree at "
            f"{worktree_info.worktree_path} (branch: "
            f"{worktree_info.branch_name}). Treat that path as "
            f"your working directory — operate within it.\n\n"
            f"{task}"
        )
        return worktree_manager, worktree_info, original_base_dirs, worktree_task

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
        if self.current_depth >= self.max_depth:
            return f"Error: Maximum nesting depth ({self.max_depth}) reached."

        if isolation and isolation not in _VALID_ISOLATION_MODES:
            return (
                f"Error: unknown isolation mode {isolation!r}. "
                f"Valid: {sorted(m for m in _VALID_ISOLATION_MODES if m)}."
            )

        if limit_err := self._check_agent_limit(1):
            return limit_err

        try:
            shared = self.pool.get(agent_name)
        except KeyError as e:
            return str(e)
        # Shallow-copy per spawn. Agno ``Agent`` instances hold per-run
        # state on the object itself — ``run_id``, ``session_id``,
        # ``run_response``. The pool caches one instance per agent name
        # and hands it to every caller. Under concurrent spawns of the
        # same specialist (broadcast mode in real chat, parallel test
        # cases in evals) those callers race on the shared state and
        # ``acontinue_run`` ends up looking for a run_id from a
        # different concurrent run — Agno raises "No runs found for
        # run ID …". Shallow copy keeps the heavy refs (model, tools,
        # db, instructions) shared while giving each spawn its own
        # mutable run-state slots.
        agent = copy.copy(shared)

        defn = self.pool.get_definition(agent_name)
        agent_desc = defn.description if defn else ""
        agent_tools = ", ".join(defn.tools) if defn and defn.tools else "none"

        # Plugin-shipped agents force their own isolation
        # regardless of what the caller asked for — CC parity
        # row 37. ``AgentDefinition.force_isolation`` is set to
        # ``"worktree"`` by the plugin loader; user / project
        # agents leave it ``None`` and respect the caller's arg.
        # ``isinstance(..., str)`` guards against duck-typed
        # test mocks where ``defn.force_isolation`` might be a
        # MagicMock — a truthy MagicMock would otherwise sneak
        # into ``isolation`` and silently disable the worktree
        # branch.
        forced = getattr(defn, "force_isolation", None) if defn is not None else None
        if isinstance(forced, str) and forced:
            isolation = forced

        # ── Isolation: per-spawn worktree ─────────────────────
        isolation_result = self._setup_isolation(agent, agent_name, isolation, task)
        if isinstance(isolation_result, str):
            # Worktree creation failed — surface the error string
            # as the spawn result so the agent sees the reason
            # and can fall back to a non-isolated retry.
            return isolation_result
        worktree_manager, worktree_info, original_base_dirs, worktree_task = isolation_result

        await self._fire_hook("SubagentStart", {"agent_name": agent_name, "task": task[:500]})

        # One stable id per spawn — stamped on every orchestrate event
        # for this run so the FE routes them all into the same
        # team-progress card. See ``_emit`` in ``_run_agent_streaming``.
        card_id = uuid.uuid4().hex[:8]
        if self._on_progress:
            with contextlib.suppress(Exception):
                self._on_progress(
                    {
                        "type": "agent_started",
                        "agent_path": agent_name,
                        "agent": agent_name,
                        "parent": None,
                        # FE Retry UI pre-fills its textarea with this.
                        "task": task,
                        "card_id": card_id,
                    }
                )

        # Spawn deadline — without this a hung specialist (model
        # provider stalls, network partition) ties up the parent
        # forever. ``sub_team_timeout`` is the existing knob.
        spawn_timeout = self.settings.orchestration.sub_team_timeout
        try:
            start = time.monotonic()
            result, activity = await asyncio.wait_for(
                _run_agent_streaming(
                    agent,
                    worktree_task,
                    on_progress=self._on_progress,
                    hitl_coordinator=self._hitl_coordinator,
                    agent_path=[agent_name],
                    card_id=card_id,
                ),
                timeout=spawn_timeout,
            )
            elapsed = time.monotonic() - start

            await self._fire_hook(
                "SubagentStop", {"agent_name": agent_name, "result_preview": result[:500]}
            )

            return _format_spawn_result(
                agent_name=agent_name,
                agent_desc=agent_desc,
                agent_tools=agent_tools,
                task=task,
                elapsed=elapsed,
                result=result,
                activity=activity,
                worktree_footer=_finalize_worktree(
                    worktree_manager, worktree_info, original_base_dirs
                ),
            )
        except asyncio.TimeoutError:
            error = (
                f"Sub-agent '{agent_name}' exceeded spawn timeout "
                f"({spawn_timeout}s) and was aborted. The model provider "
                "likely stalled mid-stream."
            )
            await self._fire_hook("SubagentStop", {"agent_name": agent_name, "error": error})
            _finalize_worktree(worktree_manager, worktree_info, original_base_dirs)
            return error
        except Exception as e:
            error = f"Error running sub-agent '{agent_name}': {e}"
            await self._fire_hook("SubagentStop", {"agent_name": agent_name, "error": error})
            _finalize_worktree(worktree_manager, worktree_info, original_base_dirs)
            return error

    async def spawn_team(self, task: str, agent_names: str, mode: str = "coordinate") -> str:
        """Create and run a sub-team for a specific subtask.

        Args:
            task: The subtask description.
            agent_names: Comma-separated agent names from the pool.
            mode: Team mode: "coordinate", "route", "broadcast", or "tasks".

        Returns:
            The team's response with activity log.
        """
        if self.current_depth >= self.max_depth:
            return f"Error: Maximum nesting depth ({self.max_depth}) reached."

        names = [n.strip() for n in agent_names.split(",") if n.strip()]
        if limit_err := self._check_agent_limit(len(names)):
            return limit_err
        if not names:
            return "Error: No agent names provided."
        if len(names) == 1:
            return await self.spawn_agent(task, names[0])

        try:
            team_or_err = self._build_sub_team(names, mode)
            if isinstance(team_or_err, str):
                return team_or_err
            team, mode = team_or_err

            member_lines = []
            for n in names:
                defn = self.pool.get_definition(n)
                desc = defn.description[:60] if defn else ""
                member_lines.append(f"  - {n}: {desc}")

            await self._fire_hook(
                "SubagentStart",
                {"agent_name": f"team({','.join(names)})", "task": task[:500], "mode": mode},
            )

            spawn_timeout = self.settings.orchestration.sub_team_timeout
            start = time.monotonic()
            team_label = f"team({mode}:{','.join(names)})"
            # One card_id per team spawn — see ``spawn_agent`` for the
            # rationale. ``_run_team_streaming`` stamps it onto every
            # emitted event so the FE can attach them all to a single
            # team-progress card no matter what interleaves on the wire.
            card_id = uuid.uuid4().hex[:8]
            result, activity = await asyncio.wait_for(
                _run_team_streaming(
                    team,
                    task,
                    on_progress=self._on_progress,
                    hitl_coordinator=self._hitl_coordinator,
                    agent_path=[team_label],
                    card_id=card_id,
                ),
                timeout=spawn_timeout,
            )
            elapsed = time.monotonic() - start

            await self._fire_hook(
                "SubagentStop",
                {"agent_name": f"team({','.join(names)})", "result_preview": result[:500]},
            )

            return _format_team_result(
                names=names,
                mode=mode,
                member_lines=member_lines,
                task=task,
                elapsed=elapsed,
                result=result,
                activity=activity,
            )
        except asyncio.TimeoutError:
            error = (
                f"Sub-team {team_label!r} exceeded spawn timeout "
                f"({spawn_timeout}s) and was aborted."
            )
            await self._fire_hook(
                "SubagentStop", {"agent_name": f"team({','.join(names)})", "error": error}
            )
            return error
        except Exception as e:
            error = f"Error running sub-team: {e}"
            await self._fire_hook(
                "SubagentStop", {"agent_name": f"team({','.join(names)})", "error": error}
            )
            return error

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
        tool_list = [t.strip() for t in tools.split(",") if t.strip()]
        try:
            self.pool.register_ephemeral(
                name=name, description=description, system_prompt=system_prompt, tools=tool_list
            )
            return f"Created ephemeral agent '{name}': {description}. Use spawn_agent(task, '{name}') to delegate."
        except (ValueError, RuntimeError) as e:
            return f"Error creating agent: {e}"


def reset_agent_counter(session_id: str) -> None:
    with _agent_counter_lock:
        _agent_counters.pop(session_id, None)
