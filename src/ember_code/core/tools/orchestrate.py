"""OrchestrateTools — allows agents to spawn sub-teams at runtime."""

import asyncio
import contextlib
import copy
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.hooks.executor import HookExecutor
    from ember_code.core.pool import AgentPool
    from ember_code.core.worktree import WorktreeInfo

_VALID_ISOLATION_MODES: frozenset[str] = frozenset({"", "worktree"})

logger = logging.getLogger(__name__)

_agent_counter_lock = threading.Lock()
_agent_counters: dict[str, int] = {}


def _finalize_worktree(
    manager: Any,
    info: "WorktreeInfo | None",
    original_base_dirs: dict[Any, Any],
) -> str:
    """Restore tool ``base_dir`` rebinds and clean up the
    worktree. Returns a footer string for the spawn response so
    the parent agent knows whether the worktree was reaped or
    preserved.

    Idempotent and exception-safe — designed to run inside
    ``finally``-ish paths after every spawn, isolated or not.
    Returns ``""`` when there was no worktree (the normal case)
    so callers can append unconditionally.
    """
    # Restore tool base_dirs first — the worktree dir may
    # disappear in the cleanup step below, and a stray reference
    # to it after that would point at a missing path.
    for tool, original in original_base_dirs.items():
        with contextlib.suppress(Exception):
            tool.base_dir = original
    if manager is None or info is None:
        return ""
    try:
        reaped = manager.cleanup()
    except Exception as exc:
        logger.warning("worktree cleanup failed: %s", exc)
        return (
            f"\n\nWorktree: {info.worktree_path} (branch: "
            f"{info.branch_name}) — cleanup failed: {exc}"
        )
    if reaped:
        return f"\n\nWorktree {info.branch_name} (clean) — reaped."
    return (
        f"\n\nWorktree preserved: {info.worktree_path} "
        f"(branch: {info.branch_name}) — has uncommitted changes.\n"
        f"To merge: git merge {info.branch_name}\n"
        f"To remove: git worktree remove {info.worktree_path}"
    )


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


# How many non-empty lines of streamed agent content to keep in the
# rolling "thinking" preview shown under each agent header. Matches the
# FE constant ``PREVIEW_WINDOW`` in clients/web/src/chat/model.ts — the
# BE is the source of truth for the window, the FE just renders it.
PREVIEW_WINDOW = 5
PREVIEW_LINE_MAX = 120


def _build_preview(buf: str) -> str:
    """Turn an agent's accumulated streaming text into the multi-line
    preview payload — the last PREVIEW_WINDOW non-empty lines, each
    truncated to PREVIEW_LINE_MAX chars, joined by ``\\n``.

    Returning a multi-line ``text`` is the protocol: the FE splits on
    ``\\n`` and *replaces* its preview window. That keeps the BE as the
    source of truth — Agno deltas are token-sized, so the FE used to
    fill its window with token-per-line garbage when it appended each
    delta as its own preview entry.
    """
    if not buf:
        return ""
    cleaned = buf.replace("<think>", "").replace("</think>", "")
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = lines[-PREVIEW_WINDOW:]
    truncated = [
        (ln[: PREVIEW_LINE_MAX - 1] + "…") if len(ln) > PREVIEW_LINE_MAX else ln for ln in tail
    ]
    return "\n".join(truncated)


_stream_log = __import__("logging").getLogger("ember_code.llm_calls")


async def _run_agent_streaming(
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
    from agno.run import agent as agent_events

    log: list[str] = []
    current_tool: str | None = None
    last_update: float = 0.0
    last_preview: str = ""
    # Accumulates streamed text deltas so we can extract proper *lines*
    # for the FE preview window. Agno's RunContentEvent.content is a
    # delta — often a single token — so taking ``splitlines()[-1]`` of
    # the delta alone yields token-per-line junk; we have to buffer
    # the stream and slice the tail.
    content_buf: str = ""
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
    # Dot-path used as the agent's stable identifier on the FE tree.
    agent_path_id: str = ".".join(path) if path else "root"

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
        nonlocal current_tool, last_update, last_preview, content_buf
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
                _emit(
                    {
                        "type": "run_error",
                        "agent_path": agent_path_id,
                        "error": "paused: no HITL bridge available",
                    }
                )
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
            current_tool = tn
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
            tn = (te.tool_name if te else None) or current_tool or "tool"
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
            if current_tool == tn:
                current_tool = None
        elif isinstance(event, agent_events.ToolCallErrorEvent):
            te = getattr(event, "tool", None)
            tn = (te.tool_name if te else None) or current_tool or "tool"
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
            if current_tool == tn:
                current_tool = None
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
                completed_content = str(c)
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
        elif isinstance(event, agent_events.RunContentEvent):
            # Live progress preview only — final content comes from
            # ``agent.run_response.content`` after the stream ends.
            c = event.content or ""
            if c:
                content_buf += str(c)
                now = time.monotonic()
                if now - last_update > 0.5:
                    last_update = now
                    preview = _build_preview(content_buf)
                    if preview and preview != last_preview:
                        last_preview = preview
                        _emit(
                            {
                                "type": "content_preview",
                                "agent_path": agent_path_id,
                                "text": preview,
                            }
                        )
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
    from agno.run import agent as agent_events
    from agno.run import team as team_events

    log: list[str] = []
    current_tool: str | None = None
    current_agent: str = ""
    # Per-agent throttle and dedup state — keyed by agent_path_id.
    # In broadcast/coordinate mode multiple sub-agents emit content
    # deltas interleaved, so a shared ``last_preview``/``last_update``
    # would let one chatty agent suppress another's updates.
    last_update_by_agent: dict[str, float] = {}
    last_preview_by_agent: dict[str, str] = {}
    # Per-agent accumulator for streaming text — see ``content_buf`` in
    # ``_run_agent_streaming`` for why we buffer instead of looking at
    # the delta alone.
    content_buf_by_agent: dict[str, str] = {}
    current_run_id: str | None = None
    # See _run_agent_streaming for why this is required.
    current_session_id: str | None = None
    # Fallback capture — see _run_agent_streaming.
    completed_content: str = ""
    team_path_id: str = ".".join(base_path) if base_path else "team"

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
        not a shared ``current_agent`` — is critical for broadcast
        runs where multiple sub-agents emit interleaved tool events:
        every tool call carries its owning ``agent_name`` (or
        ``team_name`` for nested teams), and using that prevents the
        "all tools land on the last started agent" bug."""
        name = (
            getattr(event, "agent_name", None)
            or getattr(event, "team_name", None)
            or current_agent
            or ""
        )
        return _agent_path_for(name), name

    async def _handle(event: Any) -> Any:
        """Returns a follow-up async iterator if we resumed the run after
        a HITL pause; None otherwise."""
        nonlocal current_tool, current_agent
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
            if name and name != current_agent:
                current_agent = name
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
            _emit(
                {
                    "type": "agent_paused",
                    "agent_path": _agent_path_for(current_agent),
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
                    "agent_path": _agent_path_for(assignee or current_agent),
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
                    "agent_path": _agent_path_for(current_agent),
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
            current_tool = tn
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
            tn = (te.tool_name or current_tool or "tool") if te else (current_tool or "tool")
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
            if current_tool == tn:
                current_tool = None
        elif isinstance(event, agent_events.ToolCallErrorEvent):
            ev_path, _ev_name = _event_agent_path(event)
            te = getattr(event, "tool", None)
            tn = (te.tool_name if te else None) or current_tool or "tool"
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
            if current_tool == tn:
                current_tool = None
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
                completed_content = str(c)
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
            from ember_code.core.worktree import WorktreeManager

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
        worktree_manager = None
        worktree_info: WorktreeInfo | None = None
        original_base_dirs: dict[Any, Any] = {}
        worktree_task = task
        if isolation == "worktree":
            worktree_manager, info_or_err = self._create_isolated_worktree(agent_name)
            if worktree_manager is None:
                # Surface the failure as the spawn result — agent
                # sees the reason and can fall back to a non-
                # isolated retry.
                return info_or_err
            worktree_info = info_or_err
            original_base_dirs = self._rebind_tool_base_dirs(agent, worktree_info.worktree_path)
            # Tell the model where its sandbox is. Many tools
            # respect ``base_dir``; the few that don't (custom
            # MCP, etc.) still see the project root, so the
            # explicit instruction nudges the agent to pass
            # absolute paths within the worktree.
            worktree_task = (
                f"You are running in an isolated git worktree at "
                f"{worktree_info.worktree_path} (branch: "
                f"{worktree_info.branch_name}). Treat that path as "
                f"your working directory — operate within it.\n\n"
                f"{task}"
            )

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
            worktree_footer = _finalize_worktree(
                worktree_manager, worktree_info, original_base_dirs
            )
            return (
                f"[Agent: {agent_name}] {agent_desc}\n"
                f"[Tools: {agent_tools}]\n"
                f"[Task: {task}]\n"
                f"[Time: {elapsed:.1f}s]\n\n"
                f"Activity:\n{activity_log}\n\n"
                f"Response:\n{result}"
                f"{error_section}"
                f"{worktree_footer}"
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
