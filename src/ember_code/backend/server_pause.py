"""HITL pause pipeline + subagent-aware stream muxer.

Extracted from :mod:`ember_code.backend.server`:

* :func:`stream_with_subagent_hitl` — the multiplexer that
  forwards a team's Agno event stream to the FE while also
  pumping sub-agent coordinator pauses in real time.
* :func:`build_subagent_run_paused` — packages sub-agent
  requirement entries into the ``RunPaused`` shape the FE
  dialog expects.
* :func:`handle_pause` — runs each paused requirement through
  the shared ``PermissionEvaluator`` first (plan-mode /
  acceptEdits / bypass / deny short-circuit) and only pauses
  for the ones the policy left undecided.
* :func:`drop_pending_for_run` — sweep pending entries when a
  run completes/errors without going through
  ``resolve_hitl_batch``.
* :func:`periodic_checkpoint` / :func:`checkpoint_session` —
  best-effort mid-run session persistence so ``--continue``
  after a crash surfaces the in-flight ``RunOutput``.

All functions take ``backend: BackendServer`` as the first
argument. :class:`BackendServer` holds one-line delegates.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from ember_code.core.config.permission_eval import (
    PermissionDecision,
    explain_deny,
)
from ember_code.protocol import messages as msg
from ember_code.protocol.agno_events import (
    RUN_COMPLETED_EVENTS,
    RUN_ERROR_EVENTS,
    RUN_PAUSED_EVENTS,
    TOOL_NAMES,
)
from ember_code.protocol.serializer import serialize_event

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)

_LLM_LOGGER = logging.getLogger("ember_code.llm_calls")

_HITL_TRACE_PATH = Path(os.path.expanduser("~/.ember/hitl_trace.log"))


def _hitl_trace(text: str) -> None:
    """Direct-write trace bypassing the logging stack.

    Kept because the standard logging pipeline is silenced in
    several test / production configurations and this file has
    been the fastest way to confirm the mux is running. Cheap:
    one flushed write per pump iteration, no rotation needed
    (dev machines only).
    """
    try:
        _HITL_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_HITL_TRACE_PATH, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {text}\n")
    except Exception:
        pass


async def stream_with_subagent_hitl(
    backend: "BackendServer",
    team_stream: AsyncIterator[Any],
) -> AsyncIterator[msg.Message]:
    """Multiplex a team's event stream with the sub-agent coordinator.

    The team's stream and the sub-agent HITL coordinator are two
    independent producers of messages we need to forward to the FE:

    * ``team_stream`` is whatever Agno is currently driving — the
      initial ``team.arun`` call from ``run_message``, or a
      ``team.acontinue_run`` resumption from ``resolve_hitl``.
    * The coordinator wakes whenever a sub-agent (running inside a
      ``spawn_agent`` tool) hits a ``RunPausedEvent``. We have to
      surface that pause to the FE as a ``RunPaused`` message so the
      dialog appears.

    Both paths must run concurrently with the team stream; otherwise
    a sub-agent that pauses while the parent is still streaming
    events would have its requirement sitting in the coordinator
    forever with no one to forward it. Centralising this here means
    ``run_message`` AND ``resolve_hitl`` both get the multiplexer —
    a previous version had it only in ``run_message`` so any sub-
    agent spawn that happened during a resumed run (parent paused
    for top-level Bash, user approved, parent resumed and then
    spawned an architect) silently dropped the architect's pauses.

    The team's own ``RunPausedEvent`` (parent pauses for its own
    tool) terminates this stream and is forwarded as ``RunPaused``;
    the FE then routes resolution back through ``resolve_hitl``,
    which calls this helper again with the resumed stream.
    """
    sub_hitl = backend._session.sub_agent_hitl
    agno_queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    async def _drain_team() -> None:
        try:
            async for event in team_stream:
                await agno_queue.put(("event", event))
        except Exception as e:
            await agno_queue.put(("error", e))
        finally:
            await agno_queue.put(("done", SENTINEL))

    async def _drain_subagent_hitl() -> None:
        _hitl_trace(f"_stream_mux: drain STARTED (coord_id={id(sub_hitl)})")
        try:
            while True:
                await sub_hitl.new_arrival.wait()
                entries = sub_hitl.list_new_pending()
                _hitl_trace(f"_stream_mux: drain woke, {len(entries)} entries")
                if entries:
                    await agno_queue.put(("subagent_pause", entries))
                    _hitl_trace(f"_stream_mux: drain enqueued {[rid for rid, _ in entries]}")
        except asyncio.CancelledError:
            _hitl_trace("_stream_mux: drain cancelled")
            return

    _hitl_trace(f"_stream_mux: starting (coord_id={id(sub_hitl)})")
    # Hold the team-drain reference so the task isn't GC'd mid-run
    # (asyncio only weakly references background tasks). We
    # deliberately don't cancel it in ``finally`` — see comment
    # below.
    _team_task = asyncio.create_task(_drain_team())  # noqa: F841
    sub_task = asyncio.create_task(_drain_subagent_hitl())

    try:
        while True:
            kind, payload = await agno_queue.get()
            if kind == "done":
                return
            if kind == "error":
                raise payload
            if kind == "subagent_pause":
                entries = payload
                rp = build_subagent_run_paused(entries)
                _LLM_LOGGER.info(
                    "subagent_hitl: yielding RunPaused to FE with %d req(s)",
                    len(entries),
                )
                yield rp
                continue
            event = payload
            if isinstance(event, RUN_PAUSED_EVENTS):
                pause_msgs, auto_resolved, paused_run_id = handle_pause(backend, event)
                for pause_msg in pause_msgs:
                    yield pause_msg
                if pause_msgs:
                    # Mixed pause: some reqs still need the user.
                    # Stash the auto-resolved ones so
                    # ``resolve_hitl_batch`` can merge them into the
                    # eventual ``acontinue_run`` call.
                    if auto_resolved and paused_run_id:
                        backend._auto_resolved_requirements.setdefault(paused_run_id, []).extend(
                            auto_resolved
                        )
                    return
                if auto_resolved and paused_run_id:
                    # Every req was decided by the evaluator. Resume
                    # the team immediately without ever bothering
                    # the FE — this is the plan-mode / acceptEdits /
                    # bypass / deny-rule short-circuit.
                    team = backend._session.main_team
                    _LLM_LOGGER.info(
                        "auto-resuming run_id=%s with %d evaluator-resolved req(s)",
                        paused_run_id,
                        len(auto_resolved),
                    )
                    async for proto in stream_with_subagent_hitl(
                        backend,
                        team.acontinue_run(
                            run_id=paused_run_id,
                            session_id=backend._session.session_id,
                            requirements=auto_resolved,
                            stream=True,
                            stream_events=True,
                        ),
                    ):
                        yield proto
                    return
                # No requirements at all — defensive; shouldn't
                # normally happen but don't strand the stream.
                return
            # If a run completes/errors without going through HITL
            # resolution (e.g. tool didn't require approval, or the
            # whole run was cancelled) sweep any stale pending
            # requirements for that run_id so they don't pile up on
            # the session. ``resolve_hitl_batch`` already pops the
            # entries it resolves; this catches the "user closed the
            # UI mid-pause and the run later wrapped up" path.
            run_finished = isinstance(event, RUN_COMPLETED_EVENTS + RUN_ERROR_EVENTS)
            if run_finished:
                finished_run_id = getattr(event, "run_id", None)
                if finished_run_id:
                    drop_pending_for_run(backend, finished_run_id)
            # Local name distinct from the outer-loop ``proto``
            # so mypy doesn't complain about widening a Message
            # binding to Message | None.
            serialized = serialize_event(event)
            if serialized is not None:
                yield serialized
            if run_finished:
                # Drain post-run broadcasts (e.g. ``plan_submitted``
                # queued by ``exit_plan_mode``). The push fires AFTER
                # the run's content has flushed so the PlanCard lands
                # at the bottom of the agent's reply, not mid-stream.
                # ``finished_run_id`` is stamped onto each payload so
                # the FE can key approve/dismiss RPCs by run_id.
                drain = getattr(backend._session, "drain_post_run_broadcasts", None)
                if drain is not None:
                    try:
                        drain(run_id=finished_run_id)
                    except Exception as exc:
                        logger.debug("post-run broadcast drain raised: %s", exc)
    except asyncio.TimeoutError:
        yield msg.Error(text="Request timed out — the model took too long to respond.")
    except Exception as e:
        yield msg.Error(text=str(e))
    finally:
        sub_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sub_task
        # Don't cancel team_task — the team's stream may still have
        # a paused tool we want to drive to completion via
        # ``resolve_hitl``. The team task naturally exits when the
        # team's stream ends or errors.


def build_subagent_run_paused(entries: list) -> msg.Message:
    """Wrap a batch of sub-agent coordinator entries in a ``RunPaused``.

    The FE renders the confirmation dialog only when it sees
    ``RunPaused``; bare ``HITLRequest`` falls through. Sub-agent pauses
    match the FE's expected shape so the same dialog flow applies.
    Resolution still routes through the coordinator (not the main
    team's ``acontinue_run``) — see ``resolve_hitl``.
    """
    requirements = []
    for req_id, entry in entries:
        req = entry.requirement
        tool_exec = getattr(req, "tool_execution", None)
        raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
        requirements.append(
            msg.HITLRequest(
                requirement_id=req_id,
                tool_name=raw_name,
                friendly_name=TOOL_NAMES.get(raw_name, raw_name),
                tool_args=dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {}),
                agent_path=list(getattr(entry, "agent_path", []) or []),
            )
        )
    # ``run_id`` here is the sub-agent's id; the FE doesn't currently
    # use it for sub-agent pauses (it routes through ``resolve_hitl``
    # which picks the coordinator path), but we forward it for logs.
    sub_run_id = entries[0][1].run_id if entries else ""
    return msg.RunPaused(run_id=sub_run_id, requirements=requirements)


async def periodic_checkpoint(
    backend: "BackendServer",
    team: Any,
    interval: float = 3.0,
) -> None:
    """Background loop that snapshots the session every ``interval`` seconds.

    Agno's streaming runs don't write to disk between
    RunStarted and RunCompleted. For a pure text-only response
    (no tools, so no tool-completed event for us to hook), the
    in-flight ``RunOutput`` would never reach SQLite — a crash
    mid-stream would lose the user's prompt AND the partial
    response. The pre-persistence in ``_run_message_locked``
    saves the prompt unconditionally; this task takes care of
    the partial response by forcing ``asave_session`` on a
    cadence.

    Cancellation is the normal stop signal — the streaming
    loop cancels this task in its finally. We swallow
    ``CancelledError`` cleanly and exit; anything else is
    logged but never propagated.
    """
    try:
        while True:
            await asyncio.sleep(interval)
            # Route through the instance method so per-instance
            # patches (used by ``test_crash_survival`` etc.) still
            # intercept — the free function is the canonical body
            # but callers may swap the method for a spy.
            await backend._checkpoint_session(team)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.debug("periodic checkpoint task crashed: %s", exc)


async def checkpoint_session(backend: "BackendServer", team: Any) -> None:
    """Force Agno to persist the in-flight session to SQLite.

    Agno saves the session blob only at end-of-run via
    ``_cleanup_and_store``. Mid-run, ``upsert_run`` writes to the
    *in-memory* session but never touches disk. A process crash
    between user message and run completion therefore loses
    everything Agno did so far — tool calls, partial responses,
    intermediate planning. By snapshotting the cached session
    after every tool completion we keep the disk copy within one
    tool-result of the live state, so ``--continue`` after a
    crash surfaces the in-flight ``RunOutput`` (with
    ``status=running``) and the agent can pick up where it left
    off. On a clean completion, Agno's own end-of-run save
    overwrites these snapshots via upsert semantics — no
    explicit cleanup needed.

    Best-effort: a transient persistence failure must not abort
    the live stream. If the session blob is unavailable (e.g.
    Agno hasn't created the cached_session yet on a very early
    event) we log and move on.
    """
    del backend  # only ``team`` is used; ``backend`` kept for consistency
    try:
        session = getattr(team, "cached_session", None)
        if session is None:
            return
        await team.asave_session(session)
    except Exception as exc:
        logger.debug("incremental session checkpoint failed: %s", exc)


def drop_pending_for_run(backend: "BackendServer", run_id: str) -> None:
    """Remove any pending HITL entries tied to a finished run.

    Called when a run completes/errors without going through
    ``resolve_hitl_batch`` (which would've popped them). Guards
    against per-session accumulation of dead requirement entries
    when the user closes the pause UI and the run later wraps up
    on its own.
    """
    stale = [
        rid
        for rid, (_req, rid_run) in backend._pending_requirements.items()
        if rid_run == run_id
    ]
    for rid in stale:
        backend._pending_requirements.pop(rid, None)
    if stale:
        logger.debug(
            "_drop_pending_for_run: dropped %d stale requirement(s) for run_id=%s",
            len(stale),
            run_id,
        )
    # Same sweep for the auto-resolved bucket — if the run finished
    # without ``resolve_hitl_batch`` draining it, drop the entry so
    # it doesn't leak across sessions.
    auto_bucket = getattr(backend, "_auto_resolved_requirements", None)
    if auto_bucket is not None:
        auto_bucket.pop(run_id, None)


def _apply_auto_decision(
    req: Any,
    decision: str,
    raw_name: str,
    run_id: str | None,
    reason: str,
) -> bool:
    """Auto-confirm or auto-reject an Agno requirement in response
    to a permission-evaluator verdict. Returns True when the
    decision was applied cleanly (caller appends to
    ``auto_resolved``), False when the underlying call raised
    (caller falls back to the user prompt).

    Decoupled from :func:`handle_pause`'s main loop to consolidate
    the confirm/reject try/except-with-fallback pattern that was
    duplicated across the two branches.
    """
    try:
        if decision == "confirm":
            req.confirm()
        else:  # "reject"
            req.reject(note=f"Blocked: {reason}")
    except Exception as exc:
        logger.warning(
            "auto-%s raised for %s: %s — falling back to user prompt",
            decision,
            raw_name,
            exc,
        )
        return False
    if decision == "confirm":
        logger.info("Auto-confirmed %s by permission policy (run_id=%s)", raw_name, run_id)
    else:
        logger.info("Auto-rejected %s (%s) run_id=%s", raw_name, reason, run_id)
    return True


def handle_pause(
    backend: "BackendServer",
    event: Any,
) -> tuple[list[msg.Message], list[Any], str | None]:
    """Convert a RunPausedEvent into protocol messages and store requirements.

    Returns ``(messages, auto_resolved_reqs, run_id)``:

    * ``messages`` — what to forward to the FE. Either a single
      ``RunPaused`` (when at least one requirement still needs the
      user) or empty (when the evaluator decided every req).
    * ``auto_resolved_reqs`` — Agno requirement objects that the
      evaluator already confirmed or rejected. Caller resumes Agno
      with these via ``acontinue_run`` (all-auto case) or stashes
      them on ``_auto_resolved_requirements`` for the eventual
      ``resolve_hitl_batch`` (mixed case).
    * ``run_id`` — the paused run; needed by the resume.

    Why do this here: Agno's ``requires_confirmation`` gate pauses
    every "ask"-level tool indiscriminately. Without this pre-step,
    plan-mode-deny, acceptEdits-allow, bypass-allow, and ``deny:``
    rules can never short-circuit the dialog — the user sees an
    approval prompt for tools the policy already decided about.
    """
    run_id_raw = getattr(event, "run_id", None)
    run_id = str(run_id_raw) if run_id_raw else None
    evaluator = getattr(backend._session, "permission_evaluator", None)
    requirements: list[msg.HITLRequest] = []
    auto_resolved: list[Any] = []

    for req in getattr(event, "active_requirements", []) or []:
        req_id = str(uuid.uuid4())[:8]
        tool_exec = getattr(req, "tool_execution", None)
        raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
        tool_args = dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {})

        auto_decision: str | None = None  # "confirm" | "reject" | None
        if evaluator is not None:
            try:
                pd = evaluator.evaluate(raw_name, tool_args)
            except Exception as exc:
                logger.warning(
                    "permission_evaluator.evaluate(%s) raised %s — falling back to user prompt",
                    raw_name,
                    exc,
                )
            else:
                if pd is PermissionDecision.DENY:
                    auto_decision = "reject"
                elif pd is PermissionDecision.ALLOW:
                    auto_decision = "confirm"

        if auto_decision is not None:
            reason = (
                explain_deny(evaluator, raw_name, tool_args)
                if auto_decision == "reject"
                else ""
            )
            if _apply_auto_decision(req, auto_decision, raw_name, run_id, reason):
                auto_resolved.append(req)
                continue

        # Defer: ask the user as before.
        backend._pending_requirements[req_id] = (req, run_id)
        requirements.append(
            msg.HITLRequest(
                requirement_id=req_id,
                tool_name=raw_name,
                friendly_name=TOOL_NAMES.get(raw_name, raw_name),
                tool_args=tool_args,
            )
        )

    messages: list[msg.Message] = []
    if requirements:
        messages.append(msg.RunPaused(run_id=run_id or "", requirements=requirements))
    return messages, auto_resolved, run_id
