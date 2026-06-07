"""OrchestrateTools — allows agents to spawn sub-teams at runtime."""

import asyncio
import contextlib
import copy
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.hooks.executor import HookExecutor
    from ember_code.core.pool import AgentPool

logger = logging.getLogger(__name__)

_agent_counter_lock = threading.Lock()
_agent_counters: dict[str, int] = {}


def _format_args(tool_args: dict | None) -> str:
    if not tool_args:
        return ""
    parts = []
    for k, v in list(tool_args.items())[:2]:
        val = str(v).replace("\n", " ")
        if len(val) > 30:
            val = val[:27] + "..."
        parts.append(f"{k}={val}")
    return ", ".join(parts)


def _preview(result: Any, limit: int = 60) -> str:
    if result is None:
        return ""
    s = str(result).replace("\n", " ").strip()
    return s[:limit] + "..." if len(s) > limit else s


_stream_log = __import__("logging").getLogger("ember_code.llm_calls")


async def _run_agent_streaming(
    agent: Any,
    task: str,
    on_progress: Any = None,
    hitl_coordinator: Any = None,
    agent_path: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Stream an agent run, collecting activity log. Returns (response, log).

    If ``hitl_coordinator`` is provided, ``RunPausedEvent``s from the
    sub-agent are surfaced through it so the user can confirm/deny in
    the TUI; the run is then resumed via ``acontinue_run`` and we keep
    iterating its events. Without a coordinator, pauses are ignored and
    the sub-agent's tools will return empty results.

    ``agent_path`` is the chain of agent names from the main orchestrator
    down to the agent being run here (e.g. ``["architect"]``). It rides
    along with each pause requirement so the FE dialog can name the
    specialist that's asking for permission, not just the tool.
    """
    path = list(agent_path or [])
    from agno.run import agent as agent_events

    log: list[str] = []
    current_tool: str | None = None
    last_update: float = 0.0
    last_preview: str = ""
    current_run_id: str | None = None
    # Captured from RunStartedEvent. ``acontinue_run`` requires this —
    # without it Agno errors with "No runs found for run ID …" because
    # runs are keyed by ``(run_id, session_id)`` in the session DB.
    current_session_id: str | None = None
    # Backup capture of the final answer from ``RunCompletedEvent``.
    # ``aget_run_output`` is the canonical source, but the async DB
    # write that backs it sometimes hasn't flushed by the time the
    # stream ends — and the architect's last RunCompletedEvent already
    # carries the full content we need. Belt and suspenders.
    completed_content: str = ""

    def _log(line: str) -> None:
        log.append(line)
        if on_progress:
            with contextlib.suppress(Exception):
                on_progress(line)

    async def _handle(event: Any) -> Any:
        """Drive progress reporting and HITL bridging.

        We don't try to reassemble the model's text from streaming events —
        Agno already accumulates that into ``agent.run_response.content``
        when the run ends. We just watch the stream for tool-call lifecycle
        (for activity log lines), HITL pauses (for the bridge), and content
        deltas (for the live progress preview).

        Returns a follow-up async iterator when we resumed the run after
        a pause; otherwise None.
        """
        nonlocal current_tool, last_update, last_preview
        nonlocal current_run_id, current_session_id, completed_content

        # Capture ``run_id`` / ``session_id`` from *any* event that
        # carries them — not just ``RunStartedEvent``. Agno only yields
        # ``RunStartedEvent`` when ``stream_events=True``, which we
        # don't pass to keep specialist streams quiet, so a
        # ``RunStartedEvent``-only capture would leave both fields
        # ``None`` and our ``aget_run_output(run_id, session_id)``
        # lookup would silently return ``None``. Every Agno run event
        # has these fields, so latching onto the first non-empty value
        # we see is sufficient and stable across pause/resume.
        if not current_run_id:
            ev_run_id = getattr(event, "run_id", None)
            if ev_run_id:
                current_run_id = ev_run_id
        if not current_session_id:
            ev_session_id = getattr(event, "session_id", None)
            if ev_session_id:
                current_session_id = ev_session_id

        if isinstance(event, agent_events.RunStartedEvent):
            return None

        if isinstance(event, agent_events.RunPausedEvent):
            reqs = getattr(event, "active_requirements", None) or []
            if hitl_coordinator is None or not reqs:
                _log("  │  ⚠ paused: no HITL bridge available")
                return None
            run_id = getattr(event, "run_id", "") or current_run_id or ""
            session_id = getattr(event, "session_id", None) or current_session_id
            req_ids = []
            for req in reqs:
                req_id = await hitl_coordinator.push_requirement(
                    req, run_id=run_id, agent_path=path
                )
                req_ids.append(req_id)
            # One concise activity line per pause batch — the user has
            # the dialog itself, and the parent agent doesn't need our
            # internal req_ids in its context. Detail the count when
            # the model batched multiple tool calls into one pause.
            _log(
                f"  │  ⏸ awaiting approval ({len(req_ids)} tools)"
                if len(req_ids) > 1
                else "  │  ⏸ awaiting approval"
            )
            try:
                for req_id in req_ids:
                    await hitl_coordinator.wait_resolved(req_id)
            finally:
                for req_id in req_ids:
                    hitl_coordinator.cleanup(req_id)
            # ``run_id`` + ``session_id`` is enough for Agno to find the
            # paused run — but only when the agent has a ``db=`` so the
            # run was actually persisted. The pool wires ``InMemoryDb``
            # into every specialist for exactly this reason.
            return agent.acontinue_run(
                run_id=run_id,
                session_id=session_id,
                requirements=reqs,
                stream=True,
            )

        if isinstance(event, agent_events.ToolCallStartedEvent):
            te = event.tool
            tn = (te.tool_name or "tool") if te else "tool"
            ta = te.tool_args if te else {}
            current_tool = tn
            _log(f"  │  ├─ {tn}({_format_args(ta)})")
        elif isinstance(event, agent_events.ToolCallCompletedEvent):
            te = event.tool
            r = getattr(te, "result", None) if te else None
            if current_tool:
                _log(f"  │  │  └─ {_preview(r)}")
                current_tool = None
        elif isinstance(event, agent_events.ToolCallErrorEvent):
            err = str(getattr(event, "error", "?"))
            _log(f"  │  │  └─ ERROR: {err[:60]}")
            current_tool = None
        elif isinstance(event, agent_events.RunErrorEvent):
            err = str(getattr(event, "content", "") or getattr(event, "error", "?"))
            _log(f"  │  ⚠ RUN ERROR: {err[:200]}")
            _stream_log.info("subagent_stream: RunErrorEvent path=%s err=%s", path, err[:300])
        elif isinstance(event, agent_events.RunCompletedEvent):
            # Last RunCompletedEvent of the run carries the full final
            # answer — keep the latest non-empty value as a fallback in
            # case the DB-backed lookup below comes up empty.
            c = getattr(event, "content", None)
            if c:
                completed_content = str(c)
        elif isinstance(event, agent_events.RunContentEvent):
            # Live progress preview only — final content comes from
            # ``agent.run_response.content`` after the stream ends.
            c = event.content or ""
            if c and on_progress:
                now = time.monotonic()
                if now - last_update > 0.5:
                    last_update = now
                    line = c.replace("<think>", "").replace("</think>", "").splitlines()
                    line = line[-1].strip() if line else ""
                    if line and line != last_preview:
                        last_preview = line
                        preview = line[:120] + "..." if len(line) > 120 else line
                        with contextlib.suppress(Exception):
                            on_progress(f"  │  ✎ {preview}")
        return None

    # Outer loop drives the active stream. When _handle returns a follow-up
    # iterator (after pause+resume), we switch to it. Sub-agent runs can
    # pause arbitrarily many times; this loop handles each one.
    stream = agent.arun(task, stream=True)
    while stream is not None:
        next_stream = None
        async for event in stream:
            follow_up = await _handle(event)
            if follow_up is not None:
                next_stream = follow_up
                break
        stream = next_stream

    # Read the final answer back from Agno's session DB. ``Agent`` does
    # not expose a ``run_response`` attribute (we tried — it errors with
    # AttributeError); the supported way to fetch the canonical
    # ``RunOutput`` after a streaming run completes is
    # ``aget_run_output(run_id, session_id)``. Fall through to the
    # streamed ``RunCompletedEvent.content`` if that comes up empty —
    # in practice we've seen the DB-backed lookup return ``None`` for
    # MiniMax-driven specialists even though the run completed cleanly.
    _stream_log.info(
        "subagent_stream: stream ended path=%s run_id=%s session_id=%s completed_content_len=%d",
        path,
        current_run_id,
        current_session_id,
        len(completed_content),
    )
    final = ""
    try:
        run_output = None
        if current_run_id and current_session_id:
            run_output = await agent.aget_run_output(
                run_id=current_run_id, session_id=current_session_id
            )
        if run_output is None and current_session_id:
            run_output = await agent.aget_last_run_output(session_id=current_session_id)
        rr_content = getattr(run_output, "content", None) if run_output else None
        rr_status = getattr(run_output, "status", None) if run_output else None
        _stream_log.info(
            "subagent_stream: db lookup path=%s found=%s status=%s content_len=%d",
            path,
            run_output is not None,
            rr_status,
            len(str(rr_content)) if rr_content else 0,
        )
        if rr_content:
            final = str(rr_content).replace("<think>", "").replace("</think>", "").strip()
    except Exception as exc:
        _stream_log.info(
            "subagent_stream: failed to read run_output path=%s err=%s",
            path,
            exc,
        )
    if not final and completed_content:
        final = completed_content.replace("<think>", "").replace("</think>", "").strip()
        _stream_log.info(
            "subagent_stream: used RunCompletedEvent fallback path=%s len=%d",
            path,
            len(final),
        )
    return final, log


async def _run_team_streaming(
    team: Any,
    task: str,
    on_progress: Any = None,
    hitl_coordinator: Any = None,
    agent_path: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Stream a team run, collecting activity log. Returns (response, log).

    Mirrors ``_run_agent_streaming``'s pause-handling: ``RunPausedEvent``
    from any team member is forwarded through the coordinator so the user
    can confirm/deny via the TUI, then we resume via ``acontinue_run``.

    ``agent_path`` is the chain of names down to this team. We pull each
    paused member's name from the requirement's ``member_agent_name`` (set
    by Agno when the requirement originates from a team member) and
    append it to ``path`` so the FE shows ``team → member`` not just
    ``team``.
    """
    base_path = list(agent_path or [])
    from agno.run import agent as agent_events
    from agno.run import team as team_events

    log: list[str] = []
    current_tool: str | None = None
    current_agent: str = ""
    last_update: float = 0.0
    last_preview: str = ""
    current_run_id: str | None = None
    # See _run_agent_streaming for why this is required.
    current_session_id: str | None = None
    # Fallback capture — see _run_agent_streaming.
    completed_content: str = ""

    def _log(line: str) -> None:
        log.append(line)
        if on_progress:
            with contextlib.suppress(Exception):
                on_progress(line)

    async def _handle(event: Any) -> Any:
        """Returns a follow-up async iterator if we resumed the run after
        a HITL pause; None otherwise."""
        nonlocal current_tool, current_agent, last_update, last_preview
        nonlocal current_run_id, current_session_id, completed_content

        # See ``_run_agent_streaming`` for why we latch onto run_id /
        # session_id from any event rather than just RunStartedEvent.
        if not current_run_id:
            ev_run_id = getattr(event, "run_id", None)
            if ev_run_id:
                current_run_id = ev_run_id
        if not current_session_id:
            ev_session_id = getattr(event, "session_id", None)
            if ev_session_id:
                current_session_id = ev_session_id

        if isinstance(event, (agent_events.RunStartedEvent, team_events.RunStartedEvent)):
            name = getattr(event, "agent_name", None) or getattr(event, "team_name", None)
            if name and name != current_agent:
                current_agent = name
                _log(f"  ├─ [{name}]")
            return None

        if isinstance(event, agent_events.RunPausedEvent):
            reqs = getattr(event, "active_requirements", None) or []
            if hitl_coordinator is None or not reqs:
                _log("  │  ⚠ paused: no HITL bridge available")
                return None
            run_id = getattr(event, "run_id", "") or current_run_id or ""
            session_id = getattr(event, "session_id", None) or current_session_id
            req_ids = []
            for req in reqs:
                # If Agno tagged the requirement with a member name (set
                # for team-internal pauses), append it to the path so the
                # FE shows e.g. ``review-team → security``.
                member = getattr(req, "member_agent_name", None)
                req_path = base_path + [member] if member else base_path
                req_id = await hitl_coordinator.push_requirement(
                    req, run_id=run_id, agent_path=req_path
                )
                req_ids.append(req_id)
            _log(
                f"  │  ⏸ awaiting approval ({len(req_ids)} tools)"
                if len(req_ids) > 1
                else "  │  ⏸ awaiting approval"
            )
            try:
                for req_id in req_ids:
                    await hitl_coordinator.wait_resolved(req_id)
            finally:
                for req_id in req_ids:
                    hitl_coordinator.cleanup(req_id)
            return team.acontinue_run(
                run_id=run_id,
                session_id=session_id,
                requirements=reqs,
                stream=True,
            )

        if isinstance(event, team_events.TaskCreatedEvent):
            title = getattr(event, "title", "")
            assignee = getattr(event, "assignee", "")
            _log(f"  ┌─ TASK: {title}")
            if assignee:
                _log(f"  │  assigned to: {assignee}")
        elif isinstance(event, team_events.TaskUpdatedEvent):
            status = getattr(event, "status", "")
            icon = {"completed": "✓", "failed": "✗", "running": "…"}.get(status, "·")
            _log(f"  │  {icon} {status}")
        elif isinstance(event, team_events.TaskIterationStartedEvent):
            _log(f"  ╞═ Iteration {getattr(event, 'iteration', 0)}")
        elif isinstance(
            event, (agent_events.ToolCallStartedEvent, team_events.ToolCallStartedEvent)
        ):
            te = event.tool
            tn = (te.tool_name or "tool") if te else "tool"
            ta = te.tool_args if te else {}
            current_tool = tn
            _log(f"  │  ├─ {tn}({_format_args(ta)})")
        elif isinstance(
            event, (agent_events.ToolCallCompletedEvent, team_events.ToolCallCompletedEvent)
        ):
            te = event.tool
            r = getattr(te, "result", None) if te else None
            if current_tool:
                _log(f"  │  │  └─ {_preview(r)}")
                current_tool = None
        elif isinstance(event, (agent_events.ToolCallErrorEvent, team_events.ToolCallErrorEvent)):
            err = str(getattr(event, "error", "?"))
            _log(f"  │  │  └─ ERROR: {err[:60]}")
            current_tool = None
        elif isinstance(event, (agent_events.RunErrorEvent, team_events.RunErrorEvent)):
            err = str(getattr(event, "content", "?"))
            _log(f"  │  └─ ERROR: {err[:60]}")
        elif isinstance(event, (agent_events.RunCompletedEvent, team_events.RunCompletedEvent)):
            c = getattr(event, "content", None)
            if c:
                completed_content = str(c)
        elif isinstance(event, (agent_events.RunContentEvent, team_events.RunContentEvent)):
            # Live progress preview only — final content comes from
            # ``team.run_response.content`` after the stream ends.
            c = event.content or ""
            if c and on_progress:
                now = time.monotonic()
                if now - last_update > 0.5:
                    last_update = now
                    line = c.replace("<think>", "").replace("</think>", "").splitlines()
                    line = line[-1].strip() if line else ""
                    if line and line != last_preview:
                        last_preview = line
                        preview = line[:120] + "..." if len(line) > 120 else line
                        with contextlib.suppress(Exception):
                            on_progress(f"  │  ✎ {preview}")
        return None

    stream = team.arun(task, stream=True)
    while stream is not None:
        next_stream = None
        async for event in stream:
            follow_up = await _handle(event)
            if follow_up is not None:
                next_stream = follow_up
                break
        stream = next_stream

    # See ``_run_agent_streaming`` for why we read via Agno's session-DB
    # API rather than a hypothetical ``team.run_response`` attribute,
    # and why we keep the streamed RunCompletedEvent content as a
    # fallback.
    final = ""
    try:
        run_output = None
        if current_run_id and current_session_id:
            run_output = await team.aget_run_output(
                run_id=current_run_id, session_id=current_session_id
            )
        if run_output is None and current_session_id:
            run_output = await team.aget_last_run_output(session_id=current_session_id)
        rr_content = getattr(run_output, "content", None) if run_output else None
        if rr_content:
            final = str(rr_content).replace("<think>", "").replace("</think>", "").strip()
    except Exception as exc:
        _stream_log.info(
            "subteam_stream: failed to read run_output path=%s err=%s",
            base_path,
            exc,
        )
    if not final and completed_content:
        final = completed_content.replace("<think>", "").replace("</think>", "").strip()
    return final, log


class OrchestrateTools(Toolkit):
    """Tools for agents to spawn sub-teams from the agent pool."""

    def __init__(
        self,
        pool: "AgentPool",
        settings: "Settings",
        current_depth: int = 0,
        hook_executor: "HookExecutor | None" = None,
        session_id: str = "",
        hitl_coordinator: Any = None,
    ):
        super().__init__(name="ember_orchestrate")
        self.pool = pool
        self.settings = settings
        self.current_depth = current_depth
        self.max_depth = settings.orchestration.max_nesting_depth
        self._hook_executor = hook_executor
        self._session_id = session_id
        self._max_agents = settings.orchestration.max_total_agents
        self._on_progress: Any = None
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

    async def spawn_agent(self, task: str, agent_name: str) -> str:
        """Run a single agent from the pool on a subtask.

        Args:
            task: The subtask description for the agent.
            agent_name: Name of the agent to spawn (from the pool).

        Returns:
            The agent's response with activity log.
        """
        if self.current_depth >= self.max_depth:
            return f"Error: Maximum nesting depth ({self.max_depth}) reached."

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

        await self._fire_hook("SubagentStart", {"agent_name": agent_name, "task": task[:500]})

        if self._on_progress:
            with contextlib.suppress(Exception):
                self._on_progress(f"  ├─ [{agent_name}]")

        # Spawn deadline — without this a hung specialist (model
        # provider stalls, network partition) ties up the parent
        # forever. ``sub_team_timeout`` is the existing knob.
        spawn_timeout = self.settings.orchestration.sub_team_timeout
        try:
            start = time.monotonic()
            result, activity = await asyncio.wait_for(
                _run_agent_streaming(
                    agent,
                    task,
                    on_progress=self._on_progress,
                    hitl_coordinator=self._hitl_coordinator,
                    agent_path=[agent_name],
                ),
                timeout=spawn_timeout,
            )
            elapsed = time.monotonic() - start

            await self._fire_hook(
                "SubagentStop", {"agent_name": agent_name, "result_preview": result[:500]}
            )

            activity_log = "\n".join(activity) if activity else "  (no tool calls)"
            # Detect a sub-agent that hit a run-level error mid-stream
            # (e.g. model API failure). Surface it explicitly so the
            # parent agent doesn't guess "looks cut off" — it sees the
            # actual error and can react (retry, switch tactic, etc.).
            run_errors = [line for line in activity if "RUN ERROR" in line]
            error_section = ""
            if run_errors:
                error_section = (
                    "\n\nWARNING: This sub-agent terminated with a run error — "
                    "the response below is partial. Consider retrying, or proceed "
                    "with the partial result if it's sufficient.\n" + "\n".join(run_errors)
                )
            return (
                f"[Agent: {agent_name}] {agent_desc}\n"
                f"[Tools: {agent_tools}]\n"
                f"[Task: {task}]\n"
                f"[Time: {elapsed:.1f}s]\n\n"
                f"Activity:\n{activity_log}\n\n"
                f"Response:\n{result}"
                f"{error_section}"
            )
        except asyncio.TimeoutError:
            error = (
                f"Sub-agent '{agent_name}' exceeded spawn timeout "
                f"({spawn_timeout}s) and was aborted. The model provider "
                "likely stalled mid-stream."
            )
            await self._fire_hook("SubagentStop", {"agent_name": agent_name, "error": error})
            return error
        except Exception as e:
            error = f"Error running sub-agent '{agent_name}': {e}"
            await self._fire_hook("SubagentStop", {"agent_name": agent_name, "error": error})
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
            from agno.team.team import Team

            from ember_code.core.config.models import ModelRegistry

            members = []
            for name in names:
                try:
                    # Per-spawn shallow copy — see ``spawn_agent`` for
                    # the rationale. Members of a sub-team also race on
                    # shared per-run state otherwise.
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
            # Share the session's DB so the team's runs are persisted
            # in the same store as the main team's. Without it Agno's
            # ``team.acontinue_run(run_id, session_id)`` (called when a
            # member pauses for HITL during broadcast/coordinate mode)
            # fails with "No runs found for run ID …" — exactly the
            # symptom that turned ``broadcast_*`` and
            # ``single_specialist_*`` cases into 60s case-timeouts in
            # the eval. Same fix as ``pool.py`` for specialist agents.
            pool_db = getattr(self.pool, "_db", None)
            if pool_db is not None:
                team_kwargs["db"] = pool_db
            if mode == "tasks":
                team_kwargs["max_iterations"] = self.settings.orchestration.max_task_iterations

            team = Team(**team_kwargs)

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
            result, activity = await asyncio.wait_for(
                _run_team_streaming(
                    team,
                    task,
                    on_progress=self._on_progress,
                    hitl_coordinator=self._hitl_coordinator,
                    agent_path=[team_label],
                ),
                timeout=spawn_timeout,
            )
            elapsed = time.monotonic() - start

            await self._fire_hook(
                "SubagentStop",
                {"agent_name": f"team({','.join(names)})", "result_preview": result[:500]},
            )

            activity_log = "\n".join(activity) if activity else "  (no activity)"
            return (
                f"[Team: {', '.join(names)}] (mode: {mode})\n"
                f"[Members:\n" + "\n".join(member_lines) + "]\n"
                f"[Task: {task}]\n"
                f"[Time: {elapsed:.1f}s]\n\n"
                f"Activity:\n{activity_log}\n\n"
                f"Response:\n{result}"
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
