"""Team stream handler — one Agno team, one stream, N members.

Concrete subclass of :class:`BaseStreamHandler` for the
``spawn_team`` path. Sibling of :class:`SubAgentStreamHandler`.

The team case adds:

* A ``_should_hide_coordinator`` predicate that drops the team's own
  lifecycle/content/tool events (they overlap with each member's,
  and the team-progress card already represents the team).
* Per-member agent-path derivation via ``_agent_path_for`` /
  ``_event_agent_path`` — each Agno event carries its owning agent
  name, so tool events attribute to the right specialist even when
  members emit interleaved during broadcast mode.
* Per-member throttled content-preview via
  :meth:`TeamStreamState.append_content_delta`.
* Task lifecycle events (``TaskCreated`` / ``TaskUpdated`` /
  ``TaskIterationStarted``) — team-mode-only.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.run import agent as agent_events
from agno.run import team as team_events

from ember_code.core.tools.orchestrate_events import (
    TASK_STATUS_ICONS,
    AgentCompletedEvent,
    AgentPausedEvent,
    AgentStartedEvent,
    ContentPreviewEvent,
    EventAppender,
    HitlCoordinatorProtocol,
    LogSymbols,
    OnProgress,
    RunErrorEvent,
    SubAgentRegistry,
    TaskCreatedEvent,
    TaskUpdatedEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from ember_code.core.tools.orchestrate_preview import PREVIEWS
from ember_code.core.tools.orchestrate_stream_handler import BaseStreamHandler
from ember_code.core.tools.subagent_stream import TeamStreamState

_stream_log = logging.getLogger("ember_code.llm_calls")


class TeamStreamHandler(BaseStreamHandler[TeamStreamState]):
    """Drive one Agno team stream. Constructor mirrors the old
    ``run_team_streaming`` function signature."""

    def __init__(
        self,
        team: Any,
        task: str,
        *,
        on_progress: OnProgress | None = None,
        hitl_coordinator: HitlCoordinatorProtocol | None = None,
        agent_path: list[str] | None = None,
        card_id: str = "",
        subagent_registry: SubAgentRegistry,
        event_appender: EventAppender | None = None,
    ) -> None:
        super().__init__(
            team,
            task,
            on_progress=on_progress,
            hitl_coordinator=hitl_coordinator,
            agent_path=agent_path,
            card_id=card_id,
            subagent_registry=subagent_registry,
            event_appender=event_appender,
        )
        self._base_path: list[str] = list(agent_path or [])
        self.state = TeamStreamState(
            team_path_id=".".join(self._base_path) if self._base_path else "team",
            card_id=card_id,
        )

    # ── Dispatch ───────────────────────────────────────────────────
    async def _handle(self, event: Any) -> Any:
        """Return a follow-up async iterator on pause+resume, else
        ``None``."""
        self._latch_run_ids(event)

        # Hide the team coordinator's own lifecycle/content/tool
        # events — see :meth:`_should_hide_coordinator`.
        if self._should_hide_coordinator(event):
            return None

        if isinstance(event, agent_events.RunStartedEvent):
            self._on_agent_run_started(event)
            return None
        if isinstance(event, agent_events.RunPausedEvent):
            return await self._on_run_paused(event)
        if isinstance(event, team_events.TaskCreatedEvent):
            self._on_task_created(event)
            return None
        if isinstance(event, team_events.TaskUpdatedEvent):
            self._on_task_updated(event)
            return None
        if isinstance(event, team_events.TaskIterationStartedEvent):
            self._on_task_iteration_started(event)
            return None
        if isinstance(event, agent_events.ToolCallStartedEvent):
            self._on_tool_started(event)
            return None
        if isinstance(event, agent_events.ToolCallCompletedEvent):
            self._on_tool_completed(event)
            return None
        if isinstance(event, agent_events.ToolCallErrorEvent):
            self._on_tool_error(event)
            return None
        if isinstance(event, agent_events.RunErrorEvent):
            self._on_run_error(event)
            return None
        if isinstance(event, agent_events.RunCompletedEvent):
            self._on_agent_run_completed(event)
            return None
        if isinstance(event, agent_events.RunContentEvent):
            self._on_run_content(event)
            return None
        return None

    # ── Helpers ────────────────────────────────────────────────────
    @staticmethod
    def _should_hide_coordinator(event: Any) -> bool:
        """Drop the team's own lifecycle/content/tool events.

        Agno's Team has its own model that does routing/planning in
        coordinate/route/tasks modes and emits ``team_events.*``
        alongside the workers' ``agent_events.*``. Surfacing the
        coordinator as a 4th row alongside three real workers
        confuses users — the team-progress card itself already
        represents the team. ``TaskCreated`` / ``TaskUpdated`` are
        team_events too, but attributed to a member assignee, so
        they stay (handled below).
        """
        return isinstance(
            event,
            (
                team_events.RunStartedEvent,
                team_events.RunCompletedEvent,
                team_events.RunContentEvent,
                team_events.RunErrorEvent,
                team_events.ToolCallStartedEvent,
                team_events.ToolCallCompletedEvent,
                team_events.ToolCallErrorEvent,
            ),
        )

    def _agent_path_for(self, name: str | None) -> str:
        """Build the agent-path id for a team member by appending
        its name to the team's base path."""
        if not name:
            return self.state.team_path_id
        base = self._base_path
        return ".".join(base + [name]) if base else name

    def _event_agent_path(self, event: Any) -> tuple[str, str]:
        """Pull the agent identity off ANY Agno event and return
        ``(path, display_name)``.

        Reading from the event itself — not a shared
        ``state.current_agent`` — is critical for broadcast runs
        where multiple sub-agents emit interleaved tool events:
        every tool call carries its owning ``agent_name`` (or
        ``team_name`` for nested teams), and using that prevents
        the "all tools land on the last started agent" bug.
        """
        name = (
            getattr(event, "agent_name", None)
            or getattr(event, "team_name", None)
            or self.state.current_agent
            or ""
        )
        return self._agent_path_for(name), name

    # ── Per-event handlers ─────────────────────────────────────────
    def _on_agent_run_started(self, event: Any) -> None:
        state = self.state
        name = getattr(event, "agent_name", None) or getattr(event, "team_name", None)
        if not name or name == state.current_agent:
            return
        state.current_agent = name
        self._log_line(f"  {LogSymbols.T_BRANCH.value} [{name}]")
        self._emit(
            AgentStartedEvent(
                agent_path=self._agent_path_for(name),
                agent=name,
                parent=state.team_path_id,
                # Emit the run_id so the FE can target this
                # specific sub-agent for cancellation —
                # ``cancel_agent_run(run_id)`` flags this run for
                # cooperative stop while siblings keep going.
                run_id=str(getattr(event, "run_id", "") or ""),
                # The team's task is what the parent agent asked
                # the broadcast to do. Each member receives the
                # same prompt — the FE Retry UI pre-fills it so
                # the user can tweak before respawning.
                task=self._task,
            )
        )

    async def _on_run_paused(self, event: Any) -> Any:
        state = self.state
        reqs = getattr(event, "active_requirements", None) or []
        if self._hitl_coordinator is None or not reqs:
            self._log_line(
                f"  {LogSymbols.T_TRUNK.value}  "
                f"{LogSymbols.WARNING.value} paused: no HITL bridge available"
            )
            return None
        run_id = getattr(event, "run_id", "") or state.current_run_id or ""
        session_id = getattr(event, "session_id", None) or state.current_session_id
        req_ids: list[str] = []
        for req in reqs:
            # If Agno tagged the requirement with a member name (set
            # for team-internal pauses), append it to the path so
            # the FE shows e.g. ``review-team → security``.
            member = getattr(req, "member_agent_name", None)
            req_path = self._base_path + [member] if member else self._base_path
            req_id = await self._hitl_coordinator.push_requirement(
                req, run_id=run_id, agent_path=req_path
            )
            req_ids.append(req_id)
        pause_line = (
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.PAUSE.value} "
            f"awaiting approval ({len(req_ids)} tools)"
            if len(req_ids) > 1
            else f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.PAUSE.value} awaiting approval"
        )
        self._log_line(pause_line)
        self._emit(
            AgentPausedEvent(
                agent_path=self._agent_path_for(state.current_agent),
                count=len(req_ids),
            )
        )
        try:
            for req_id in req_ids:
                await self._hitl_coordinator.wait_resolved(req_id)
        finally:
            for req_id in req_ids:
                self._hitl_coordinator.cleanup(req_id)
        return self._runnable.acontinue_run(
            run_id=run_id,
            session_id=session_id,
            requirements=reqs,
            stream=True,
        )

    def _on_task_created(self, event: Any) -> None:
        title = getattr(event, "title", "")
        assignee = getattr(event, "assignee", "")
        self._log_line(f"  {LogSymbols.T_ROOT.value} TASK: {title}")
        if assignee:
            self._log_line(f"  {LogSymbols.T_TRUNK.value}  assigned to: {assignee}")
        self._emit(
            TaskCreatedEvent(
                agent_path=self._agent_path_for(assignee or self.state.current_agent),
                title=title,
                assignee=assignee,
            )
        )

    def _on_task_updated(self, event: Any) -> None:
        status = getattr(event, "status", "")
        icon = TASK_STATUS_ICONS.get(status, "·")
        self._log_line(f"  {LogSymbols.T_TRUNK.value}  {icon} {status}")
        self._emit(
            TaskUpdatedEvent(
                agent_path=self._agent_path_for(self.state.current_agent),
                status=status,
            )
        )

    def _on_task_iteration_started(self, event: Any) -> None:
        self._log_line(f"  {LogSymbols.T_JOIN.value} Iteration {getattr(event, 'iteration', 0)}")

    def _on_tool_started(self, event: Any) -> None:
        ev_path, _name = self._event_agent_path(event)
        te = event.tool
        tn = (te.tool_name or "tool") if te else "tool"
        ta = te.tool_args if te else {}
        args_preview = PREVIEWS.format_args(ta)
        self.state.current_tool = tn
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_BRANCH.value} {tn}({args_preview})"
        )
        tc_id = getattr(te, "tool_call_id", None) if te else None
        self._emit(
            ToolStartedEvent(
                agent_path=ev_path,
                tool=tn,
                tool_call_id=tc_id,
                args=args_preview,
            )
        )

    def _on_tool_completed(self, event: Any) -> None:
        state = self.state
        ev_path, _name = self._event_agent_path(event)
        te = event.tool
        r = getattr(te, "result", None) if te else None
        tn = (
            (te.tool_name or state.current_tool or "tool") if te else (state.current_tool or "tool")
        )
        tc_id = getattr(te, "tool_call_id", None) if te else None
        result_preview = PREVIEWS.format_result(r)
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_TRUNK.value}  "
            f"{LogSymbols.T_LEAF.value} {result_preview}"
        )
        self._emit(
            ToolCompletedEvent(
                agent_path=ev_path,
                tool=tn,
                tool_call_id=tc_id,
                result=result_preview,
                is_error=False,
            )
        )
        if state.current_tool == tn:
            state.current_tool = None

    def _on_tool_error(self, event: Any) -> None:
        state = self.state
        ev_path, _name = self._event_agent_path(event)
        te = getattr(event, "tool", None)
        tn = (te.tool_name if te else None) or state.current_tool or "tool"
        tc_id = getattr(te, "tool_call_id", None) if te else None
        err = str(getattr(event, "error", "?"))
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_TRUNK.value}  "
            f"{LogSymbols.T_LEAF.value} ERROR: {err[:60]}"
        )
        self._emit(
            ToolCompletedEvent(
                agent_path=ev_path,
                tool=tn,
                tool_call_id=tc_id,
                result=err[:200],
                is_error=True,
            )
        )
        if state.current_tool == tn:
            state.current_tool = None

    def _on_run_error(self, event: Any) -> None:
        ev_path, _name = self._event_agent_path(event)
        err = str(getattr(event, "content", "?"))
        self._log_line(f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_LEAF.value} ERROR: {err[:60]}")
        self._emit(
            RunErrorEvent(
                agent_path=ev_path,
                error=err[:400],
            )
        )

    def _on_agent_run_completed(self, event: Any) -> None:
        state = self.state
        ev_path, _name = self._event_agent_path(event)
        content = getattr(event, "content", None)
        state.record_completion(content)
        # Pull per-agent token totals off ``event.metrics`` so the
        # team-progress card can show "N tokens" per agent and
        # sum them in the header.
        mt = getattr(event, "metrics", None)
        self._emit(
            AgentCompletedEvent(
                agent_path=ev_path,
                is_error=False,
                input_tokens=int(getattr(mt, "input_tokens", 0) or 0) if mt else 0,
                output_tokens=int(getattr(mt, "output_tokens", 0) or 0) if mt else 0,
                reasoning_tokens=int(getattr(mt, "reasoning_tokens", 0) or 0) if mt else 0,
            )
        )

    def _on_run_content(self, event: Any) -> None:
        # Live progress preview only — final content comes from
        # ``team.run_response.content`` after the stream ends.
        ev_path, _name = self._event_agent_path(event)
        chunk = event.content or ""
        preview = self.state.append_content_delta(ev_path, chunk)
        if preview is None:
            return
        self._emit(
            ContentPreviewEvent(
                agent_path=ev_path,
                text=preview,
            )
        )
