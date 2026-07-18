"""Multiplex the team's Agno stream with the sub-agent coordinator.

Extracted from the previous ``backend.server_pause`` module — the
previous free ``stream_with_subagent_hitl`` function reached into
``backend._session``, ``backend._auto_resolved_requirements``, and
``backend._pending_requirements`` while spinning two background
tasks and pumping a raw 2-tuple asyncio.Queue. The class version
takes ``session`` / ``store`` / ``pause_handler`` / ``tracer`` as
constructor args (composition, not reach-back), and every queue
item is a typed :class:`MuxEvent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from ember_code.backend.hitl_tracer import HITLTracer
from ember_code.backend.pause_handler import PauseHandler
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.backend.schemas_pause import (
    MuxDone,
    MuxError,
    MuxEvent,
    SubagentPause,
    TeamStreamEvent,
)
from ember_code.protocol import messages as msg
from ember_code.protocol.agno_taxonomy import (
    RUN_COMPLETED_EVENTS,
    RUN_ERROR_EVENTS,
    RUN_PAUSED_EVENTS,
)
from ember_code.protocol.agno_tool_formatter import default_registry
from ember_code.protocol.serializer import serialize_event

logger = logging.getLogger(__name__)

_LLM_LOGGER = logging.getLogger("ember_code.llm_calls")


class HITLStreamMultiplexer:
    """Fan-in for team events + sub-agent HITL pauses.

    The team's stream and the sub-agent HITL coordinator are two
    independent producers of messages we need to forward to the FE:

    * ``team_stream`` is whatever Agno is currently driving — the
      initial ``team.arun`` call from ``run_message``, or a
      ``team.acontinue_run`` resumption from
      :meth:`HitlController.resolve_single`.
    * The coordinator wakes whenever a sub-agent (running inside a
      ``spawn_agent`` tool) hits a ``RunPausedEvent``. We have to
      surface that pause to the FE as a ``RunPaused`` message so the
      dialog appears.

    Both paths must run concurrently with the team stream; otherwise
    a sub-agent that pauses while the parent is still streaming
    events would have its requirement sitting in the coordinator
    forever with no one to forward it. Centralising this here means
    ``run_message`` AND :meth:`HitlController.resolve_single` both
    get the multiplexer — a previous version had it only in
    ``run_message`` so any sub-agent spawn that happened during a
    resumed run (parent paused for top-level Bash, user approved,
    parent resumed and then spawned an architect) silently dropped
    the architect's pauses.

    The team's own ``RunPausedEvent`` (parent pauses for its own
    tool) terminates this stream and is forwarded as ``RunPaused``;
    the FE then routes resolution back through
    :meth:`HitlController.resolve_single`, which calls this helper
    again with the resumed stream.
    """

    def __init__(
        self,
        session: Any,
        store: PendingRequirementsStore,
        pause_handler: PauseHandler,
        tracer: HITLTracer,
    ) -> None:
        """Bind the collaborators for one stream lifecycle.

        Instantiate one multiplexer per stream — the internal
        ``asyncio.Queue`` + drain tasks are per-call state, not
        shared across concurrent streams.
        """
        self._session = session
        self._store = store
        self._pause_handler = pause_handler
        self._tracer = tracer

    async def stream(self, team_stream: AsyncIterator[Any]) -> AsyncIterator[msg.Message]:
        """Drive the multiplexer for one team stream lifecycle."""
        sub_hitl = self._session.sub_agent_hitl
        agno_queue: asyncio.Queue[MuxEvent] = asyncio.Queue()

        self._tracer.trace(f"_stream_mux: starting (coord_id={id(sub_hitl)})")

        # Hold the team-drain reference so the task isn't GC'd
        # mid-run (asyncio only weakly references background
        # tasks). We deliberately don't cancel it in ``finally`` —
        # the team's stream may still have a paused tool we want to
        # drive to completion via ``resolve_hitl``; it naturally
        # exits when the team's stream ends or errors.
        team_task = asyncio.create_task(self._drain_team(team_stream, agno_queue))
        sub_task = asyncio.create_task(self._drain_subagent_hitl(sub_hitl, agno_queue))

        try:
            while True:
                event = await agno_queue.get()
                if isinstance(event, MuxDone):
                    return
                if isinstance(event, MuxError):
                    # Re-raise the drained exception so the outer
                    # try/except path yields the right ``Error``
                    # message.
                    raise event.exc
                if isinstance(event, SubagentPause):
                    rp = self.build_subagent_paused(event.entries)
                    _LLM_LOGGER.info(
                        "subagent_hitl: yielding RunPaused to FE with %d req(s)",
                        len(event.entries),
                    )
                    yield rp
                    continue
                # ``TeamStreamEvent`` — the common path.
                async for m in self._forward_team_event(event.event):
                    yield m
                if self._team_stream_should_stop(event.event):
                    return
        except asyncio.CancelledError:
            # Never swallow cancellation — the ``run_message`` outer
            # ``except asyncio.CancelledError`` (in server.py)
            # needs to fire so the user sees "Run cancelled".
            raise
        except asyncio.TimeoutError:
            yield msg.Error(text="Request timed out — the model took too long to respond.")
        except Exception as e:
            # Narrowed catch: everything except CancelledError and
            # TimeoutError. Pattern-3 audit finding preserved as
            # comment — this is the last-resort surface so the FE
            # sees a message rather than a stranded stream.
            yield msg.Error(text=str(e))
        finally:
            sub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sub_task
            # Retain the team_task reference until the ``finally``
            # returns so the GC comment above holds.
            del team_task

    # ── Drain producers ──────────────────────────────────────────────

    async def _drain_team(
        self,
        team_stream: AsyncIterator[Any],
        queue: asyncio.Queue[MuxEvent],
    ) -> None:
        """Pull events off the team's Agno stream into the queue."""
        try:
            async for event in team_stream:
                await queue.put(TeamStreamEvent(event=event))
        except Exception as e:
            await queue.put(MuxError(exc=e))
        finally:
            await queue.put(MuxDone())

    async def _drain_subagent_hitl(self, sub_hitl: Any, queue: asyncio.Queue[MuxEvent]) -> None:
        """Pump sub-agent coordinator pauses into the queue."""
        self._tracer.trace(f"_stream_mux: drain STARTED (coord_id={id(sub_hitl)})")
        try:
            while True:
                await sub_hitl.new_arrival.wait()
                entries = sub_hitl.list_new_pending()
                self._tracer.trace(f"_stream_mux: drain woke, {len(entries)} entries")
                if entries:
                    await queue.put(SubagentPause(entries=entries))
                    self._tracer.trace(f"_stream_mux: drain enqueued {[rid for rid, _ in entries]}")
        except asyncio.CancelledError:
            self._tracer.trace("_stream_mux: drain cancelled")
            return

    # ── Team event dispatch ────────────────────────────────────────

    async def _forward_team_event(self, event: Any) -> AsyncIterator[msg.Message]:
        """Route one team event through the pause / cleanup / serialize path."""
        if isinstance(event, RUN_PAUSED_EVENTS):
            async for m in self._handle_paused(event):
                yield m
            return

        run_finished = isinstance(event, RUN_COMPLETED_EVENTS + RUN_ERROR_EVENTS)
        if run_finished:
            finished_run_id = getattr(event, "run_id", None)
            if finished_run_id:
                # Sweep stale pending reqs for the finished run —
                # ``resolve_hitl_batch`` already pops the entries it
                # resolves; this catches the "user closed the UI
                # mid-pause and the run later wrapped up" path.
                self._store.sweep_run(finished_run_id)

        # ``serialized`` name distinct from a hypothetical outer
        # ``proto`` binding so mypy doesn't complain about widening
        # a Message binding to Message | None.
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
            # ``broadcast_bus`` is a Session construction invariant
            # — no defensive ``getattr`` fallback required.
            try:
                self._session.broadcast_bus.drain_post_run(run_id=getattr(event, "run_id", None))
            except Exception as exc:
                logger.debug("post-run broadcast drain raised: %s", exc)

    async def _handle_paused(self, event: Any) -> AsyncIterator[msg.Message]:
        """Route a paused team event through the evaluator and
        either resume or forward to the FE."""
        result = self._pause_handler.handle(event)
        for pause_msg in result.messages:
            yield pause_msg
        if result.messages:
            # Mixed pause: some reqs still need the user.
            # Stash the auto-resolved ones so
            # ``resolve_hitl_batch`` can merge them into the
            # eventual ``acontinue_run`` call.
            if result.auto_resolved and result.run_id:
                self._store.stash_auto_resolved(result.run_id, result.auto_resolved)
            return
        if result.auto_resolved and result.run_id:
            # Every req was decided by the evaluator. Resume the
            # team immediately without ever bothering the FE — this
            # is the plan-mode / acceptEdits / bypass / deny-rule
            # short-circuit.
            team = self._session.main_team
            _LLM_LOGGER.info(
                "auto-resuming run_id=%s with %d evaluator-resolved req(s)",
                result.run_id,
                len(result.auto_resolved),
            )
            resume_mux = HITLStreamMultiplexer(
                session=self._session,
                store=self._store,
                pause_handler=self._pause_handler,
                tracer=self._tracer,
            )
            async for proto in resume_mux.stream(
                team.acontinue_run(
                    run_id=result.run_id,
                    session_id=self._session.session_id,
                    requirements=result.auto_resolved,
                    stream=True,
                    stream_events=True,
                )
            ):
                yield proto
            return
        # No requirements at all — defensive; shouldn't normally
        # happen but don't strand the stream.
        return

    def _team_stream_should_stop(self, event: Any) -> bool:
        """The paused-event branch is expected to terminate the
        outer loop after the pause is forwarded/resumed. Everything
        else keeps the stream going."""
        return isinstance(event, RUN_PAUSED_EVENTS)

    # ── Static helpers ────────────────────────────────────────────

    @staticmethod
    def build_subagent_paused(entries: list) -> msg.Message:
        """Wrap a batch of sub-agent coordinator entries in a
        ``RunPaused``.

        The FE renders the confirmation dialog only when it sees
        ``RunPaused``; bare ``HITLRequest`` falls through. Sub-
        agent pauses match the FE's expected shape so the same
        dialog flow applies. Resolution still routes through the
        coordinator (not the main team's ``acontinue_run``) — see
        :meth:`HitlController.resolve_single`.
        """
        requirements = []
        registry = default_registry()
        for req_id, entry in entries:
            req = entry.requirement
            tool_exec = getattr(req, "tool_execution", None)
            raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
            requirements.append(
                msg.HITLRequest(
                    requirement_id=req_id,
                    tool_name=raw_name,
                    friendly_name=registry.friendly_name(raw_name),
                    tool_args=dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {}),
                    agent_path=list(getattr(entry, "agent_path", []) or []),
                )
            )
        # ``run_id`` here is the sub-agent's id; the FE doesn't
        # currently use it for sub-agent pauses (it routes through
        # ``resolve_hitl`` which picks the coordinator path), but
        # we forward it for logs.
        sub_run_id = entries[0][1].run_id if entries else ""
        return msg.RunPaused(run_id=sub_run_id, requirements=requirements)

    # Legacy alias — internal call site uses the public name.
    _build_subagent_paused = build_subagent_paused
