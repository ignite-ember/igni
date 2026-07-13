"""Sub-agent + sub-team streaming loops for :mod:`orchestrate`.

Extracted from ``orchestrate.py`` — the two long streaming
generators that drive the FE's team-progress card:

* :func:`run_agent_streaming` — spawn one specialist. Streams
  agent events, tracks HITL pauses, resumes on approve,
  finalises worktrees on completion.
* :func:`run_team_streaming` — same shape for a coordinated
  team. Threads the per-member ``agent_path`` through the FE
  event so the card shows ``team → member`` not just
  ``team``.

Both return ``(response, log)`` so the parent agent's tool
return contains the sub-agent's final text plus the
per-tool-call activity log line.

Rule 2 clean — all imports at module top; the ``_stream_log``
alias is a plain :func:`logging.getLogger` call.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from agno.run import agent as agent_events
from agno.run import team as team_events

from ember_code.core.tools.orchestrate_helpers import (
    _build_preview,
    _extract_spec_from_partial_args,
    _finalize_worktree,
    _format_args,
    _preview,
    PREVIEW_LINE_MAX,
    PREVIEW_WINDOW,
    VisualizationDeltaEvent,
)
from ember_code.core.tools.subagent_stream import SubAgentStreamState, TeamStreamState

if TYPE_CHECKING:
    from ember_code.core.tools.orchestrate import OrchestrateTools  # noqa: F401 — TYPE_CHECKING

_stream_log = logging.getLogger("ember_code.llm_calls")


def _active_subagent_runs() -> set[str]:
    """Late-lookup accessor for the ``OrchestrateTools`` class
    attribute that registers sub-agent run ids for cancellation.

    Avoids the ``orchestrate → orchestrate_streaming → orchestrate``
    import cycle a direct ``OrchestrateTools`` reference at
    module top would create. Tests still access the registry
    via ``OrchestrateTools._active_subagent_runs`` directly (the
    canonical location); this function is only used from within
    this module to add / discard entries.
    """
    from ember_code.core.tools.orchestrate import OrchestrateTools

    return OrchestrateTools._active_subagent_runs


def _append_event_hook() -> Any:
    """Late-lookup accessor for ``OrchestrateTools._append_event``.

    Same rationale as :func:`_active_subagent_runs` — breaks the
    module-level import cycle by deferring the lookup to call
    time. Returns ``None`` when the class attribute is unset
    (e.g. in unit tests that build the toolkit but not the
    session broadcast plumbing).
    """
    from ember_code.core.tools.orchestrate import OrchestrateTools

    return OrchestrateTools._append_event


async def run_agent_streaming(
    agent: Any,
    task: str,
    on_progress: Any = None,
    hitl_coordinator: Any = None,
    agent_path: list[str] | None = None,
    card_id: str = "",
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

    # Per-run state — every field this function tracks across the
    # stream loop lives on one Pydantic model. See
    # ``subagent_stream.py`` for the schema and rationale. Before the
    # extraction this function had 11 nonlocals declared across two
    # ``nonlocal`` lines in ``_handle``; each new feature added
    # another one silently. Now every field is discoverable via
    # ``SubAgentStreamState.model_fields``.
    state = SubAgentStreamState(
        agent_path_id=".".join(path) if path else "root",
        card_id=card_id,
    )
    # Convenience aliases — kept so surrounding code that reads (never
    # writes) doesn't need a rename. Assignments MUST go through
    # ``state.<field>`` so the model stays consistent.
    log = state.log
    agent_path_id = state.agent_path_id

    def _log(line: str) -> None:
        """Activity-log line — included in the parent's tool return so
        the model can recap what the sub-agent did. NOT sent to the FE."""
        log.append(line)

    def _emit(event: dict) -> None:
        """Structured event delivered to the FE so it can render a real
        tree UI instead of ASCII art. ``on_progress`` is the FE callback
        wired by server.py — it receives the dict verbatim and forwards
        it as a ``PushNotification(channel="orchestrate_event")``.

        Stamps the caller's ``card_id`` onto every event so the FE can
        route this run's events to the same team-progress card across
        info-item interleaves, page refreshes, and concurrent spawns.
        """
        if on_progress:
            if card_id:
                event["card_id"] = card_id
            with contextlib.suppress(Exception):
                on_progress(event)

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
        # Capture ``run_id`` / ``session_id`` from *any* event that
        # carries them — not just ``RunStartedEvent``. Agno only yields
        # ``RunStartedEvent`` when ``stream_events=True``, which we
        # don't pass to keep specialist streams quiet, so a
        # ``RunStartedEvent``-only capture would leave both fields
        # ``None`` and our ``aget_run_output(run_id, session_id)``
        # lookup would silently return ``None``. Every Agno run event
        # has these fields, so latching onto the first non-empty value
        # we see is sufficient and stable across pause/resume.
        if not state.current_run_id:
            ev_run_id = getattr(event, "run_id", None)
            if ev_run_id:
                state.current_run_id = ev_run_id
                # Register with the cancellation registry so a
                # user-initiated ESC / cancel_run on the top-level
                # team also reaches this sub-agent. Without this,
                # a stuck sub-agent (visualizer retrying a truncated
                # tool call, model deadlock, etc.) ignores the
                # top-level cancel and keeps burning tokens.
                _active_subagent_runs().add(state.current_run_id)
        if not state.current_session_id:
            ev_session_id = getattr(event, "session_id", None)
            if ev_session_id:
                state.current_session_id = ev_session_id
        if not state.parent_top_run_id:
            ev_parent = getattr(event, "parent_run_id", None)
            if ev_parent:
                state.parent_top_run_id = ev_parent

        if isinstance(event, agent_events.RunStartedEvent):
            return None

        if isinstance(event, agent_events.RunPausedEvent):
            reqs = getattr(event, "active_requirements", None) or []
            if hitl_coordinator is None or not reqs:
                _log("  │  ⚠ paused: no HITL bridge available")
                _emit(
                    {
                        "type": "run_error",
                        "agent_path": agent_path_id,
                        "error": "paused: no HITL bridge available",
                    }
                )
                return None
            run_id = getattr(event, "run_id", "") or state.current_run_id or ""
            session_id = getattr(event, "session_id", None) or state.current_session_id
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
            _emit(
                {
                    "type": "agent_paused",
                    "agent_path": agent_path_id,
                    "count": len(req_ids),
                }
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
            args_preview = _format_args(ta)
            tc_id = getattr(te, "tool_call_id", None) if te else None
            state.current_tool = tn
            # Visualizer final delta — the model finished streaming
            # args, ``ta["spec"]`` now holds the complete parsed
            # spec. Emit one closing ``final=True`` delta so the FE
            # can transition the card out of "streaming" mode, and
            # log to the session event log so a reload replays it.
            if tn == "visualize" and isinstance(ta, dict) and isinstance(
                ta.get("spec"), dict
            ):
                final_event = VisualizationDeltaEvent(
                    agent_path=agent_path_id,
                    spec_id=state.vis_spec_id,
                    spec_json=json.dumps(ta["spec"]),
                    final=True,
                )
                final_payload = final_event.model_dump(by_alias=True)
                _emit(final_payload)
                appender = _append_event_hook()
                if appender is not None:
                    with contextlib.suppress(Exception):
                        # Use the PARENT (top-level) run_id, not the
                        # visualizer sub-agent's own run_id — see
                        # ``parent_top_run_id`` docstring above and
                        # the ``get_chat_history`` splicing block
                        # in ``backend/server.py``.
                        await appender(
                            "visualization_delta",
                            final_payload,
                            state.parent_top_run_id or state.current_run_id or "",
                        )
            _log(f"  │  ├─ {tn}({args_preview})")
            _emit(
                {
                    "type": "tool_started",
                    "agent_path": agent_path_id,
                    "tool": tn,
                    "tool_call_id": tc_id,
                    "args": args_preview,
                }
            )
        elif isinstance(event, agent_events.ToolCallCompletedEvent):
            te = event.tool
            r = getattr(te, "result", None) if te else None
            tn = (te.tool_name if te else None) or state.current_tool or "tool"
            tc_id = getattr(te, "tool_call_id", None) if te else None
            result_preview = _preview(r)
            _log(f"  │  │  └─ {result_preview}")
            _emit(
                {
                    "type": "tool_completed",
                    "agent_path": agent_path_id,
                    "tool": tn,
                    "tool_call_id": tc_id,
                    "result": result_preview,
                    "is_error": False,
                }
            )
            if state.current_tool == tn:
                state.current_tool = None
        elif isinstance(event, agent_events.ToolCallErrorEvent):
            te = getattr(event, "tool", None)
            tn = (te.tool_name if te else None) or state.current_tool or "tool"
            tc_id = getattr(te, "tool_call_id", None) if te else None
            err = str(getattr(event, "error", "?"))
            _log(f"  │  │  └─ ERROR: {err[:60]}")
            _emit(
                {
                    "type": "tool_completed",
                    "agent_path": agent_path_id,
                    "tool": tn,
                    "tool_call_id": tc_id,
                    "result": err[:200],
                    "is_error": True,
                }
            )
            if state.current_tool == tn:
                state.current_tool = None
        elif isinstance(event, agent_events.RunErrorEvent):
            err = str(getattr(event, "content", "") or getattr(event, "error", "?"))
            _log(f"  │  ⚠ RUN ERROR: {err[:200]}")
            _emit(
                {
                    "type": "run_error",
                    "agent_path": agent_path_id,
                    "error": err[:400],
                }
            )
            _stream_log.info("subagent_stream: RunErrorEvent path=%s err=%s", path, err[:300])
        elif isinstance(event, agent_events.RunCompletedEvent):
            # Last RunCompletedEvent of the run carries the full final
            # answer — keep the latest non-empty value as a fallback in
            # case the DB-backed lookup below comes up empty.
            c = getattr(event, "content", None)
            if c:
                state.completed_content = str(c)
            # Emit ``agent_completed`` with the run's metrics so the FE
            # can surface per-agent token totals. Agno's
            # ``event.metrics`` is a ``Metrics`` object with the same
            # fields the top-level RunCompleted carries.
            mt = getattr(event, "metrics", None)
            _emit(
                {
                    "type": "agent_completed",
                    "agent_path": agent_path_id,
                    "is_error": False,
                    "input_tokens": int(getattr(mt, "input_tokens", 0) or 0) if mt else 0,
                    "output_tokens": int(getattr(mt, "output_tokens", 0) or 0) if mt else 0,
                    "reasoning_tokens": int(getattr(mt, "reasoning_tokens", 0) or 0) if mt else 0,
                }
            )
            state.agent_completed_emitted = True
        elif isinstance(event, agent_events.CustomEvent):
            # Tool-arg streaming (see ``_LoggingModel.process_response_stream``).
            # When the visualizer sub-agent calls ``visualize({spec: ...})``,
            # we get a ``tool_call_input_delta`` event on every model
            # chunk with the accumulated arguments JSON. We extract
            # the ``spec`` sub-field with partial-json and forward as
            # a ``visualization_delta`` so the FE renders live.
            if getattr(event, "event", "") == "tool_call_input_delta" and getattr(
                event, "tool_name", ""
            ) == "visualize":
                args_partial = str(getattr(event, "arguments_partial", "") or "")
                if len(args_partial) > state.vis_last_emitted_len:
                    # 50ms throttle keeps the wire quiet on fast
                    # models while still feeling live. First delta
                    # always emits so the FE mounts the card early.
                    now_s = time.monotonic()
                    if (
                        state.vis_last_emitted_len == 0
                        or now_s - state.vis_last_emit_at >= 0.05
                    ):
                        spec_json = _extract_spec_from_partial_args(args_partial)
                        if spec_json:
                            state.vis_last_emitted_len = len(args_partial)
                            state.vis_last_emit_at = now_s
                            _emit(
                                VisualizationDeltaEvent(
                                    agent_path=agent_path_id,
                                    spec_id=state.vis_spec_id,
                                    spec_json=spec_json,
                                ).model_dump(by_alias=True)
                            )
        elif isinstance(event, agent_events.RunContentEvent):
            # Live progress preview only — final content comes from
            # ``agent.run_response.content`` after the stream ends.
            c = event.content or ""
            if c:
                state.content_buf += str(c)

                now = time.monotonic()
                if now - state.last_update > 0.5:
                    state.last_update = now
                    preview = _build_preview(state.content_buf)
                    if preview and preview != state.last_preview:
                        state.last_preview = preview
                        _emit(
                            {
                                "type": "content_preview",
                                "agent_path": agent_path_id,
                                "text": preview,
                            }
                        )
        return None

    # Outer loop drives the active stream. When _handle returns a
    # follow-up iterator (after pause+resume), we switch to it.
    # Sub-agent runs can pause arbitrarily many times; this loop
    # handles each one.
    #
    # The ``try/finally`` guarantees we deregister from the
    # cancellation registry no matter how the loop exits — normal
    # completion, exception, or an ESC-triggered CancelledError.
    # Without cleanup the set would grow unbounded across a session.
    try:
        stream = agent.arun(task, stream=True)
        while stream is not None:
            next_stream = None
            async for event in stream:
                follow_up = await _handle(event)
                if follow_up is not None:
                    next_stream = follow_up
                    break
            stream = next_stream
    finally:
        if state.current_run_id:
            _active_subagent_runs().discard(state.current_run_id)

    # Belt-and-suspenders ``agent_completed`` emit. Agno's specialist
    # ``arun`` doesn't yield ``RunCompletedEvent`` unless
    # ``stream_events=True`` (which we deliberately keep off to avoid
    # noisy lifecycle events on the wire). Without an
    # ``agent_completed`` emit, the FE's team-progress card keeps
    # spinning after the sub-agent has actually finished — the exact
    # bug the user just reported. Fire our own here IF the in-stream
    # handler didn't already (i.e. the caller opted into stream_events
    # and a real RunCompletedEvent arrived).
    if not state.agent_completed_emitted:
        _emit(
            {
                "type": "agent_completed",
                "agent_path": agent_path_id,
                "is_error": False,
                # Metrics unknown without RunCompletedEvent — the
                # DB-backed ``aget_run_output`` fallback below can
                # fill in the ``content`` but not the per-agent
                # token totals. FE keeps any previously-known
                # numbers.
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
            }
        )

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
        state.current_run_id,
        state.current_session_id,
        len(state.completed_content),
    )
    final = ""
    try:
        run_output = None
        if state.current_run_id and state.current_session_id:
            run_output = await agent.aget_run_output(
                run_id=state.current_run_id, session_id=state.current_session_id
            )
        if run_output is None and state.current_session_id:
            run_output = await agent.aget_last_run_output(session_id=state.current_session_id)
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
    if not final and state.completed_content:
        final = state.completed_content.replace("<think>", "").replace("</think>", "").strip()
        _stream_log.info(
            "subagent_stream: used RunCompletedEvent fallback path=%s len=%d",
            path,
            len(final),
        )
    # For the visualizer sub-agent, the tool call already streamed
    # progressive ``visualization_delta`` events to the FE (see the
    # ``CustomEvent(event="tool_call_input_delta")`` handler above),
    # so the card is already rendered. Return a short summary so the
    # PARENT agent's context doesn't get polluted with the raw spec
    # JSON — the parent just quotes the summary to the user.
    if path and path[-1] == "visualizer":
        final = "Emitted visualization to the client."
    return final, log


async def run_team_streaming(
    team: Any,
    task: str,
    on_progress: Any = None,
    hitl_coordinator: Any = None,
    agent_path: list[str] | None = None,
    card_id: str = "",
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

    # Per-run state — every field this function tracks across the
    # stream loop lives on one Pydantic model. See
    # ``subagent_stream.py::TeamStreamState`` for the schema.
    # Mirrors ``_run_agent_streaming``'s ``SubAgentStreamState``
    # pattern; before this refactor the function had 9 nonlocals
    # spread across two ``nonlocal`` lines. Now every field is
    # discoverable via ``TeamStreamState.model_fields``.
    state = TeamStreamState(
        team_path_id=".".join(base_path) if base_path else "team",
        card_id=card_id,
    )
    # Convenience aliases — kept so read-only references (which
    # are the majority) don't need a rename. Assignments MUST go
    # through ``state.<field>`` so the model stays consistent.
    log = state.log
    team_path_id = state.team_path_id
    last_update_by_agent = state.last_update_by_agent
    last_preview_by_agent = state.last_preview_by_agent
    content_buf_by_agent = state.content_buf_by_agent

    def _log(line: str) -> None:
        """Activity-log line for the parent agent's tool return."""
        log.append(line)

    def _emit(event: dict) -> None:
        """Structured event for the FE; see ``_run_agent_streaming``."""
        if on_progress:
            if card_id:
                event["card_id"] = card_id
            with contextlib.suppress(Exception):
                on_progress(event)

    def _agent_path_for(name: str | None) -> str:
        """Build the agent-path id for a team member by appending its
        name to the team's base path."""
        if not name:
            return team_path_id
        return ".".join(base_path + [name]) if base_path else name

    def _event_agent_path(event: Any) -> tuple[str, str]:
        """Pull the agent identity off ANY Agno event and return
        ``(path, display_name)``. Reading from the event itself —
        not a shared ``state.current_agent`` — is critical for broadcast
        runs where multiple sub-agents emit interleaved tool events:
        every tool call carries its owning ``agent_name`` (or
        ``team_name`` for nested teams), and using that prevents the
        "all tools land on the last started agent" bug."""
        name = (
            getattr(event, "agent_name", None)
            or getattr(event, "team_name", None)
            or state.current_agent
            or ""
        )
        return _agent_path_for(name), name

    async def _handle(event: Any) -> Any:
        """Returns a follow-up async iterator if we resumed the run after
        a HITL pause; None otherwise."""

        # See ``_run_agent_streaming`` for why we latch onto run_id /
        # session_id from any event rather than just RunStartedEvent.
        if not state.current_run_id:
            ev_run_id = getattr(event, "run_id", None)
            if ev_run_id:
                state.current_run_id = ev_run_id
                # Same cancellation registry as the single-agent path.
                _active_subagent_runs().add(state.current_run_id)
        if not state.current_session_id:
            ev_session_id = getattr(event, "session_id", None)
            if ev_session_id:
                state.current_session_id = ev_session_id

        # Hide the team coordinator's own lifecycle/content/tool events.
        # Agno's Team has its own model that does routing/planning in
        # coordinate/route/tasks modes and emits ``team_events.*``
        # alongside the workers' ``agent_events.*``. Surfacing the
        # coordinator as a 4th row alongside three real workers
        # confuses users — the team-progress card itself already
        # represents the team. ``TaskCreated``/``TaskUpdated`` are
        # team_events too, but attributed to a member assignee, so
        # they stay (handled below).
        if isinstance(
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
        ):
            return None

        if isinstance(event, agent_events.RunStartedEvent):
            name = getattr(event, "agent_name", None) or getattr(event, "team_name", None)
            if name and name != state.current_agent:
                state.current_agent = name
                _log(f"  ├─ [{name}]")
                _emit(
                    {
                        "type": "agent_started",
                        "agent_path": _agent_path_for(name),
                        "agent": name,
                        "parent": team_path_id,
                        # Emit the run_id so the FE can target this
                        # specific sub-agent for cancellation —
                        # ``cancel_agent_run(run_id)`` flags this run
                        # for cooperative stop while siblings keep
                        # going.
                        "run_id": str(getattr(event, "run_id", "") or ""),
                        # The team's task is what the parent agent asked
                        # the broadcast to do. Each member receives the
                        # same prompt — the FE Retry UI pre-fills it so
                        # the user can tweak before respawning.
                        "task": task,
                    }
                )
            return None

        if isinstance(event, agent_events.RunPausedEvent):
            reqs = getattr(event, "active_requirements", None) or []
            if hitl_coordinator is None or not reqs:
                _log("  │  ⚠ paused: no HITL bridge available")
                return None
            run_id = getattr(event, "run_id", "") or state.current_run_id or ""
            session_id = getattr(event, "session_id", None) or state.current_session_id
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
            _emit(
                {
                    "type": "agent_paused",
                    "agent_path": _agent_path_for(state.current_agent),
                    "count": len(req_ids),
                }
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
            _emit(
                {
                    "type": "task_created",
                    "agent_path": _agent_path_for(assignee or state.current_agent),
                    "title": title,
                    "assignee": assignee,
                }
            )
        elif isinstance(event, team_events.TaskUpdatedEvent):
            status = getattr(event, "status", "")
            icon = {"completed": "✓", "failed": "✗", "running": "…"}.get(status, "·")
            _log(f"  │  {icon} {status}")
            _emit(
                {
                    "type": "task_updated",
                    "agent_path": _agent_path_for(state.current_agent),
                    "status": status,
                }
            )
        elif isinstance(event, team_events.TaskIterationStartedEvent):
            _log(f"  ╞═ Iteration {getattr(event, 'iteration', 0)}")
        elif isinstance(event, agent_events.ToolCallStartedEvent):
            ev_path, _ev_name = _event_agent_path(event)
            te = event.tool
            tn = (te.tool_name or "tool") if te else "tool"
            ta = te.tool_args if te else {}
            args_preview = _format_args(ta)
            state.current_tool = tn
            _log(f"  │  ├─ {tn}({args_preview})")
            # ``tool_call_id`` lets the FE close out the right card on
            # completion when many tool calls overlap. Agno stamps a
            # unique id on every tool execution.
            tc_id = getattr(te, "tool_call_id", None) if te else None
            _emit(
                {
                    "type": "tool_started",
                    "agent_path": ev_path,
                    "tool": tn,
                    "tool_call_id": tc_id,
                    "args": args_preview,
                }
            )
        elif isinstance(event, agent_events.ToolCallCompletedEvent):
            ev_path, _ev_name = _event_agent_path(event)
            te = event.tool
            r = getattr(te, "result", None) if te else None
            tn = (te.tool_name or state.current_tool or "tool") if te else (state.current_tool or "tool")
            tc_id = getattr(te, "tool_call_id", None) if te else None
            result_preview = _preview(r)
            _log(f"  │  │  └─ {result_preview}")
            _emit(
                {
                    "type": "tool_completed",
                    "agent_path": ev_path,
                    "tool": tn,
                    "tool_call_id": tc_id,
                    "result": result_preview,
                    "is_error": False,
                }
            )
            if state.current_tool == tn:
                state.current_tool = None
        elif isinstance(event, agent_events.ToolCallErrorEvent):
            ev_path, _ev_name = _event_agent_path(event)
            te = getattr(event, "tool", None)
            tn = (te.tool_name if te else None) or state.current_tool or "tool"
            tc_id = getattr(te, "tool_call_id", None) if te else None
            err = str(getattr(event, "error", "?"))
            _log(f"  │  │  └─ ERROR: {err[:60]}")
            _emit(
                {
                    "type": "tool_completed",
                    "agent_path": ev_path,
                    "tool": tn,
                    "tool_call_id": tc_id,
                    "result": err[:200],
                    "is_error": True,
                }
            )
            if state.current_tool == tn:
                state.current_tool = None
        elif isinstance(event, agent_events.RunErrorEvent):
            ev_path, _ev_name = _event_agent_path(event)
            err = str(getattr(event, "content", "?"))
            _log(f"  │  └─ ERROR: {err[:60]}")
            _emit(
                {
                    "type": "run_error",
                    "agent_path": ev_path,
                    "error": err[:400],
                }
            )
        elif isinstance(event, agent_events.RunCompletedEvent):
            ev_path, _ev_name = _event_agent_path(event)
            c = getattr(event, "content", None)
            if c:
                state.completed_content = str(c)
            # Pull per-agent token totals off ``event.metrics`` so the
            # team-progress card can show "N tokens" per agent and
            # sum them in the header.
            mt = getattr(event, "metrics", None)
            _emit(
                {
                    "type": "agent_completed",
                    "agent_path": ev_path,
                    "is_error": False,
                    "input_tokens": int(getattr(mt, "input_tokens", 0) or 0) if mt else 0,
                    "output_tokens": int(getattr(mt, "output_tokens", 0) or 0) if mt else 0,
                    "reasoning_tokens": int(getattr(mt, "reasoning_tokens", 0) or 0) if mt else 0,
                }
            )
        elif isinstance(event, agent_events.RunContentEvent):
            # Live progress preview only — final content comes from
            # ``team.run_response.content`` after the stream ends.
            ev_path, _ev_name = _event_agent_path(event)
            c = event.content or ""
            if c:
                content_buf_by_agent[ev_path] = content_buf_by_agent.get(ev_path, "") + str(c)
                now = time.monotonic()
                if now - last_update_by_agent.get(ev_path, 0.0) > 0.5:
                    last_update_by_agent[ev_path] = now
                    preview = _build_preview(content_buf_by_agent[ev_path])
                    if preview and preview != last_preview_by_agent.get(ev_path):
                        last_preview_by_agent[ev_path] = preview
                        _emit(
                            {
                                "type": "content_preview",
                                "agent_path": ev_path,
                                "text": preview,
                            }
                        )
        return None

    # See the same try/finally in ``_run_agent_streaming`` for why
    # we deregister here.
    try:
        stream = team.arun(task, stream=True)
        while stream is not None:
            next_stream = None
            async for event in stream:
                follow_up = await _handle(event)
                if follow_up is not None:
                    next_stream = follow_up
                    break
            stream = next_stream
    finally:
        if state.current_run_id:
            _active_subagent_runs().discard(state.current_run_id)

    # See ``_run_agent_streaming`` for why we read via Agno's session-DB
    # API rather than a hypothetical ``team.run_response`` attribute,
    # and why we keep the streamed RunCompletedEvent content as a
    # fallback.
    final = ""
    try:
        run_output = None
        if state.current_run_id and state.current_session_id:
            run_output = await team.aget_run_output(
                run_id=state.current_run_id, session_id=state.current_session_id
            )
        if run_output is None and state.current_session_id:
            run_output = await team.aget_last_run_output(session_id=state.current_session_id)
        rr_content = getattr(run_output, "content", None) if run_output else None
        if rr_content:
            final = str(rr_content).replace("<think>", "").replace("</think>", "").strip()
    except Exception as exc:
        _stream_log.info(
            "subteam_stream: failed to read run_output path=%s err=%s",
            base_path,
            exc,
        )
    if not final and state.completed_content:
        final = state.completed_content.replace("<think>", "").replace("</think>", "").strip()
    return final, log


