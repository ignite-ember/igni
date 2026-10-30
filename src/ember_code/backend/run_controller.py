"""The main run engine — user-message dispatch + streaming.

Replaces the old free-function :mod:`ember_code.backend.server_run`
module. That module was three functions taking ``BackendServer``
as their first arg and reaching into ~17 private attributes.
This class owns all of that state as instance attributes and
delegates the pre-run pipeline concerns to four collaborators:

* :class:`PromptBuilder` — mentions + media + URL + interrupted
  summary composition.
* :class:`PendingMessageJournal` — pre-persist + drop-on-continue
  + mark-completed.
* :class:`RunHookGate` — UserPromptSubmit + Stop hook fires.
* :class:`StreamEventDispatcher` — per-event side-effects
  (token latch + tool-boundary checkpoints).

Plus one class for the httpx close/replace:

* :class:`ModelHttpClientManager` — replaces the module-level
  ``_HTTP_CLIENT_LIMITS`` + free ``close_model_http_client``.

Public API (used by ``BackendServer``):

* :meth:`_run_locked` — the async-generator body invoked by
  ``BackendServer._run_message_locked`` inside the outer
  ``_run_lock`` block. Kept single-underscored (rather than
  making it fully public) because the outer lock + task-tracking
  live on ``BackendServer`` and the two must be paired.
* :meth:`is_processing` — bool for the FE's ``get_processing``
  RPC — forwarded via ``BackendServer.processing``.
* :meth:`set_interrupted_summary` — writer used by
  :meth:`LifecycleController.detect_interrupted_run`. Takes a
  typed :class:`InterruptedRunSummary` so the summary text +
  drop ids travel as one value.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from agno.agent import Agent
from agno.team import Team

from ember_code.backend.model_http_client_manager import ModelHttpClientManager
from ember_code.backend.pending_message_journal import PendingMessageJournal
from ember_code.backend.prompt_builder import PromptBuilder
from ember_code.backend.run_hook_gate import RunHookGate
from ember_code.backend.schemas_lifecycle import InterruptedRunSummary
from ember_code.backend.schemas_run import CancelAgentRunResult, MediaAttachments, RunPhase
from ember_code.backend.session_checkpointer import SessionCheckpointer
from ember_code.backend.stream_event_dispatcher import StreamEventDispatcher
from ember_code.core.activity import background
from ember_code.core.session.queued_messages import DurableMessageQueue
from ember_code.core.tools.process_supervisor_locator import supervisors
from ember_code.core.utils.memory_files import MemoryMirror
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer
    from ember_code.core.session import Session
    from ember_code.core.session.pending_messages import PendingMessageStore


logger = logging.getLogger(__name__)


class RunController:
    """Owns the run lifecycle for a single ``BackendServer``.

    One instance per ``BackendServer`` — reused across every
    ``run_message`` call. Tracks the phase, the interrupted-run
    handoff, and drives the pre-run pipeline + streaming loop.

    Serialization + task-tracking still live on ``BackendServer``
    (the lock is exposed there because tests set it on partial
    instances built via ``__new__``); this controller owns the
    body-of-the-run only.
    """

    def __init__(
        self,
        backend: BackendServer,
        session: Session,
        pending_store: PendingMessageStore,
        http_client_manager: ModelHttpClientManager | None = None,
    ) -> None:
        self._backend = backend
        self._session = session
        self._pending_journal = PendingMessageJournal(pending_store, session.session_id)
        self._http_client_manager = http_client_manager or ModelHttpClientManager()
        self._prompt_builder = PromptBuilder(session)
        self._hook_gate = RunHookGate(session.hook_executor, session.session_id)
        # ``StreamEventDispatcher`` needs late binding for
        # ``_checkpoint_session`` because tests patch the method on
        # ``BackendServer`` AFTER the controller is constructed.
        self._dispatcher = StreamEventDispatcher(session, self._checkpoint_via_backend)
        # Lifecycle state — instance attributes rather than
        # ``BackendServer._x`` privates.
        self._phase = RunPhase.idle
        self._interrupted_summary: InterruptedRunSummary | None = None
        # Serialisation + cancellation state. Owned here (with
        # server.py exposing thin properties for the __new__-bypass
        # test fixtures) so Pattern 1 (single owner of run phase +
        # task) holds. See :attr:`run_lock` / :attr:`current_run_task`.
        self._run_lock = asyncio.Lock()
        self._current_run_task: asyncio.Task | None = None
        # The pending-message id stamped by :meth:`_run_locked`
        # before streaming starts. Held on the instance so the
        # sync :meth:`cancel_run` can mark the row as interrupted
        # without an async hop — see ``_stamp_interrupted``.
        self._pending_id_for_cancel: str | None = None

    # ── Public API used by BackendServer ────────────────────────────

    @property
    def pending_journal(self) -> PendingMessageJournal:
        """Underlying pending-message journal (kept accessible for
        ``get_pending_messages`` + ``detect_interrupted_run`` which
        still call the low-level store methods directly)."""
        return self._pending_journal

    @property
    def phase(self) -> RunPhase:
        """Current lifecycle phase — inspected by
        ``BackendServer.processing``."""
        return self._phase

    @property
    def http_client_manager(self) -> ModelHttpClientManager:
        """The httpx-client manager — exposed so tests can swap or
        spy on it without monkey-patching a module-level free
        function."""
        return self._http_client_manager

    @property
    def run_lock(self) -> asyncio.Lock:
        """The outer serialization lock guarding every ``run_message``
        invocation. Owned by the pipeline; server.py forwards to
        this attribute for its wire-facing public API."""
        return self._run_lock

    @property
    def current_run_task(self) -> asyncio.Task | None:
        """The ``asyncio.Task`` currently iterating the run body, or
        ``None`` when idle. Set/cleared inside :meth:`run_message`
        so tests inspecting the field see the same lifecycle as
        production."""
        return self._current_run_task

    def is_processing(self) -> bool:
        """True when a run is holding state a second submit would
        race on. Wire-compatible with the previous
        ``BackendServer._processing`` bool."""
        return self._phase.is_active()

    def set_interrupted_summary(self, summary: InterruptedRunSummary | None) -> None:
        """Route for ``detect_interrupted_run`` to hand off the
        one-shot summary + the pending ids that should be dropped
        on the next ``run_message``.

        The pair ``(summary_text, pending_ids_to_drop)`` is one
        semantic value — a typed
        :class:`InterruptedRunSummary` — rather than two
        positional args. The journal's drop queue is populated
        as a side-effect so the next :meth:`_run_locked` call
        drains it.
        """
        self._interrupted_summary = summary
        if summary is not None:
            self._pending_journal.queue_drops(summary.pending_ids_to_drop)

    def _consume_interrupted_note(self, text: str | None) -> str | None:
        """Render the one-shot interrupted-run note for *this* delivery, or
        ``None`` when the previous shutdown was clean.

        The recap is built at startup, before anyone has spoken; it is read by
        the model here, where ``text`` is the message the user just sent. Which
        of those two situations it is decides the closing instruction, so the
        choice is made at the point that knows — see
        :meth:`InterruptedRunSummary.render`. It previously always claimed the
        user had not sent a new message, one line above the message they had.

        Consuming is part of the contract: the summary is one-shot, so it is
        cleared whether or not a note comes back out.
        """
        summary, self._interrupted_summary = self._interrupted_summary, None
        if summary is None:
            return None
        return summary.render(awaiting_user=not (text or "").strip())

    # ── State transitions ───────────────────────────────────────────

    def _transition_to(self, phase: RunPhase) -> None:
        """Single writer for :attr:`_phase`. Every state change goes
        through this so a future audit for stray ``self._phase = X``
        assignments has one method to grep and one place to add
        guards / logging."""
        self._phase = phase

    # ── Late-binding shims ──────────────────────────────────────────
    # Route through the backend's methods so tests that
    # ``patch.object(BackendServer, "_stream_with_subagent_hitl", ...)``
    # (or the checkpoint / http-close variants) still intercept
    # AFTER this controller has been constructed. Fetching the method
    # off ``self._backend`` at call time (rather than binding it in
    # ``__init__``) gives us the late binding the tests rely on.

    async def _checkpoint_via_backend(self, team: Team) -> None:
        """Dispatch to ``backend._checkpoint_session`` at call time."""
        await self._backend._checkpoint_session(team)

    def _stream_via_backend(
        self, team_stream: AsyncGenerator[msg.Message, None]
    ) -> AsyncGenerator[msg.Message, None]:
        """Dispatch to ``backend._stream_with_subagent_hitl``. Returns
        the async generator; the caller does ``async for``."""
        return self._backend._stream_with_subagent_hitl(team_stream)

    async def _periodic_checkpoint_via_backend(self, team: Team) -> None:
        """Dispatch to ``backend._periodic_checkpoint`` at call time."""
        await self._backend._periodic_checkpoint(team)

    async def _close_http_client(self, team: Team) -> None:
        """Close the model httpx client via the manager."""
        await self._http_client_manager.close_and_replace(team)

    # ── Run entry / cancel (moved from BackendServer) ────────────────

    async def run_message(
        self, text: str, media: MediaAttachments | None
    ) -> AsyncGenerator[msg.Message, None]:
        """Streaming entry point — serialises concurrent submits and
        tracks the running task.

        Moved from ``BackendServer.run_message`` so the outer lock +
        current-task binding + Cancelled-error handling live with
        the pipeline that owns them. server.py forwards
        ``run_message`` to this method 1:1.
        """
        async with self._run_lock:
            self._current_run_task = asyncio.current_task()
            try:
                async for proto in self.run_locked(text, media):
                    yield proto
            except asyncio.CancelledError:
                yield msg.Info(text="Run cancelled by user.")
            finally:
                self._current_run_task = None
                # A cancelled run reaches none of ``_run_locked``'s
                # terminal transitions: ``CancelledError`` is a
                # BaseException, so its ``except Exception`` branch
                # doesn't fire and its ``finally`` only tears down the
                # checkpoint task + httpx client. The phase stayed
                # ``streaming`` forever, which meant ``is_processing()``
                # stayed True — the FE's ``get_processing`` reported the
                # session busy for the rest of the process's life and
                # ``SessionPool.evict_idle`` could never reclaim the
                # runtime. Mapped here rather than in the ``except``
                # above so a generator closed without ever resuming
                # (the consumer breaking out of its ``async for``, or a
                # re-raised cancel on the ``yield``) lands the same way.
                if self._phase.is_active():
                    self._transition_to(RunPhase.done)

    async def run_locked(
        self, text: str, media: MediaAttachments | None
    ) -> AsyncGenerator[msg.Message, None]:
        """Public rename of :meth:`_run_locked` — the body executed
        inside :attr:`run_lock`."""
        async for proto in self._run_locked(text, media):
            yield proto

    def cancel_run(self) -> None:
        """Cancel the currently running agent and kill any
        foreground process.

        Moved from ``BackendServer.cancel_run``. Fetches the run id
        off the main team, cancels the Agno run, and then cancels
        the outer task tracked by :attr:`current_run_task`.

        Also stamps the pending user message as an explicit
        interrupted run (``reason='cancelled'``) so the FE has a
        durable record on next render — instead of inferring
        interruption from a dangling pending row + the legacy
        ``RunStatus.running`` heuristic. The stamp is fire-and-
        forget so the cancel response doesn't wait on the disk.
        """
        self._cancel_own_foreground()

        try:
            team = self._session.main_team
            run_id = getattr(team, "run_id", None)
            if run_id:
                Agent.cancel_run(run_id)
        except Exception as exc:
            logger.debug("Failed to cancel run: %s", exc)

        self._stamp_interrupted("cancelled", last_error=None)

        task = self._current_run_task
        if task and not task.done():
            logger.info("Cancelling run task %s", task.get_name())
            task.cancel()

    def _cancel_own_foreground(self) -> None:
        """Kill the foreground shell — but only if this session
        could plausibly own it.

        ``supervisors.default()`` is process-wide and one BE process
        hosts N sessions (see :mod:`session_pool`), so the foreground
        slot is shared by every window. Cancelling an *idle* session
        used to reach into that shared slot and kill whatever another
        window was running in the foreground — someone's ``pytest``,
        ``npm run build`` or ``alembic upgrade``, in a different
        repository, with no indication why.

        A foreground shell only exists while the run that spawned it
        is blocked on it, so a session that is not streaming cannot
        be the owner and must not touch the slot. That closes the
        idle-window case; the both-sessions-streaming case still
        mis-attributes, and cannot be fixed from here — the
        supervisor would have to stamp an owning session id on the
        foreground slot (``core/tools/process_supervisor.py``).
        """
        if self._phase is not RunPhase.streaming:
            logger.debug(
                "cancel: session is %s, not streaming — leaving the shared foreground slot alone",
                self._phase,
            )
            return
        if supervisors.default().cancel_foreground():
            logger.info("Killed foreground process on cancel")

    def _stamp_interrupted(
        self,
        reason: str,
        last_error: str | None,
    ) -> None:
        """Schedule a fire-and-forget mark-interrupted on the pending row.

        No-op if no pending row is currently bound to a run
        (``_pending_id_for_cancel is None``) — that means the run
        finished cleanly or hasn't started yet. The cancel RPC is
        safe to call from either of those states.

        The task is intentionally un-awaited: the WS cancel handler
        returns to the client immediately, and the SQLite write is
        small enough that a dropped task (e.g. process exit
        immediately after cancel) is harmless — at worst the next
        boot falls back to the legacy ``RunStatus.running``
        heuristic.
        """
        pending_id = self._pending_id_for_cancel
        if pending_id is None:
            return
        try:
            asyncio.get_running_loop()
            background.start(
                self._pending_journal.mark_interrupted(pending_id, reason, last_error),
                holder="run",
                reason="recording an interrupted run",
                name=f"mark_interrupted:{reason}:{pending_id[:8]}",
            )
        except RuntimeError:
            # No running loop — happens in __new__-bypass test
            # fixtures. Skip silently; the legacy heuristic covers
            # this in tests.
            pass

    def cancel_agent_run(self, run_id: str) -> CancelAgentRunResult:
        """Cancel a specific sub-agent run by its Agno ``run_id``.

        Moved from ``BackendServer.cancel_agent_run``. Used by the
        team-progress UI when the user wants to stop one specialist
        mid-broadcast without killing the whole team.
        """
        if not run_id:
            return CancelAgentRunResult(ok=False, error="missing run_id")
        try:
            Agent.cancel_run(run_id)
            logger.info("Cancelled sub-agent run %s", run_id)
            return CancelAgentRunResult(ok=True)
        except Exception as exc:
            logger.warning("cancel_agent_run failed: %s", exc)
            return CancelAgentRunResult(ok=False, error=str(exc))

    # ── Checkpointing (moved from BackendServer) ────────────────────

    async def checkpoint(self, team: Any) -> None:
        """Force Agno to persist the in-flight session — one-shot.

        Moved from ``BackendServer._checkpoint_session``. Kept as a
        method (not a static) so ``patch.object`` on the pipeline
        intercepts the call from :meth:`periodic_checkpoint`.
        """
        await SessionCheckpointer(team).snapshot()

    async def periodic_checkpoint(self, team: Any, interval: float = 3.0) -> None:
        """Loop that snapshots the session every ``interval`` seconds.

        Moved from ``BackendServer._periodic_checkpoint``. Uses a
        callback into :meth:`checkpoint` so tests binding a spy
        onto the pipeline (or the legacy ``server._checkpoint_session``
        seam) still intercept on every tick.
        """
        checkpointer = SessionCheckpointer(team)
        await checkpointer.run_forever(
            interval=interval,
            checkpoint_hook=self._checkpoint_via_backend,
        )

    @staticmethod
    async def close_model_http_client(team: Any) -> None:
        """Thin shim over :meth:`ModelHttpClientManager.close_and_replace`.

        Static so callers (and tests) can invoke without an instance.
        Migrated from ``BackendServer._close_model_http_client``.
        """
        await ModelHttpClientManager().close_and_replace(team)

    # ── Stream outcome ──────────────────────────────────────────────

    @staticmethod
    def _failure_text(proto: msg.Message) -> str | None:
        """The failure this streamed message reports, or ``None``.

        The ``except Exception`` branch of :meth:`_run_locked` used to
        be the only thing that stamped a run interrupted, and nothing
        ever reached it: ``HITLStreamMultiplexer.stream`` catches every
        stream exception and *yields* an :class:`~msg.Error` instead of
        propagating it, and an Agno ``RunErrorEvent`` is serialized to
        :class:`~msg.RunError` on the ordinary event path. Both end the
        run as a failure, both left the loop above falling through to
        ``mark_completed`` — so the "stamp it interrupted so the FE can
        offer Retry/Discard" story only ever fired for user cancels.

        These two message types are the stream's typed failure
        contract; reading them here is what makes a failed run
        recoverable.
        """
        if isinstance(proto, msg.Error):
            return proto.text or "run failed"
        if isinstance(proto, msg.RunError):
            return proto.error or "run failed"
        return None

    # ── The locked body ─────────────────────────────────────────────

    async def _run_locked(
        self, text: str, media: MediaAttachments | None
    ) -> AsyncGenerator[msg.Message, None]:
        """The full pipeline body — invoked by
        ``BackendServer._run_message_locked`` inside the outer
        ``async with self._run_lock`` block.

        Split into named sub-methods so each concern (prompt
        assembly, hook gate, streaming loop, finalize) is one line
        of the body — the old free function was 163 LoC in one
        procedural blob.
        """
        self._transition_to(RunPhase.starting)
        team = self._session.main_team

        # Consume the one-shot interrupted-summary + drain any
        # pending-message ids queued during detect_interrupted_run.
        interrupted_summary_text = self._consume_interrupted_note(text)
        await self._pending_journal.drain_queued_drops()

        # Fire UserPromptSubmit hook, before the prompt is built from
        # the text: an extension that hooks it may change the text.
        # Blocks the run when should_continue=False; otherwise
        # optionally appends a <hook-context> block onto the prompt.
        gate = await self._hook_gate.fire_user_prompt_submit(text)
        if not gate.should_continue:
            yield msg.Error(text=gate.block_message or "Message blocked by hook.")
            self._transition_to(RunPhase.done)
            return
        if gate.text is not None:
            text = gate.text

        # Pre-run prompt assembly: mentions, media, URL, learnings,
        # timestamp + interrupted-run note.
        build = await self._prompt_builder.build(
            text, media, interrupted_summary=interrupted_summary_text
        )
        for info in build.info_messages:
            yield info
        message = build.message
        media = build.media

        if gate.context_message:
            message = f"{message}\n<hook-context>{gate.context_message}</hook-context>"

        # Guardrails, on the path the portal actually uses. They inform rather
        # than block, so the caveat is prepended and the model sees it before
        # the user text — the same call ``SessionMessageHandler.handle`` makes.
        # This path did not make it: every portal message reached ``team.arun``
        # unchecked, and agno's PII guardrail hard-raises before the model call
        # rather than telling the model anything, so there was no signal of any
        # kind here.
        guardrail_prefix = await self._session.guardrail_runner.warning_prefix(text)
        if guardrail_prefix:
            message = guardrail_prefix + message

        # Pre-persist the user message so a kill mid-stream doesn't
        # lose it. On success the row is marked completed; on crash
        # it stays pending and the next --continue boot surfaces it.
        pending_id = await self._pending_journal.record(text)
        # Stash on the instance so the sync ``cancel_run`` can
        # stamp the row as interrupted without an async hop. Cleared
        # below on natural completion + in the error branch so a
        # later cancel that arrives after completion is a no-op.
        self._pending_id_for_cancel = pending_id

        # Periodic checkpoint task — see SessionCheckpointer.run_forever
        # (BackendServer._periodic_checkpoint is the late-binding hook).
        checkpoint_task = background.daemon(
            self._periodic_checkpoint_via_backend(team), name="run-checkpoint"
        )

        media_kwargs = media.to_kwargs() if media is not None else {}
        self._transition_to(RunPhase.streaming)
        try:
            stream_failure: str | None = None
            async for proto in self._stream_via_backend(
                team.arun(message, stream=True, **media_kwargs)
            ):
                yield proto
                await self._dispatcher.handle(proto, team)
                stream_failure = self._failure_text(proto) or stream_failure
            if stream_failure is not None:
                # The stream reported a failure instead of raising —
                # see :meth:`_failure_text`. Same bookkeeping as the
                # ``except`` branch below, minus the re-raise: the
                # FE has already been handed the error message, so
                # raising here would only cost it its ``StreamEnd``.
                self._pending_id_for_cancel = None
                await self._pending_journal.mark_interrupted(
                    pending_id, "errored", last_error=stream_failure
                )
                self._transition_to(RunPhase.errored)
                return
            # Natural end-of-run — mark the pre-persisted user
            # message as completed.
            await self._pending_journal.mark_completed(pending_id)
            self._pending_id_for_cancel = None
            self._transition_to(RunPhase.finalizing)
        except Exception as exc:
            # Error mid-run: stamp the pending row as an explicit
            # interrupted run so the FE can recover (show a banner
            # with Retry/Discard) on next render. We do this BEFORE
            # re-raising so the mark survives even if the upstream
            # WS handler aborts on the exception.
            self._pending_id_for_cancel = None
            await self._pending_journal.mark_interrupted(pending_id, "errored", last_error=str(exc))
            self._transition_to(RunPhase.errored)
            raise
        finally:
            checkpoint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await checkpoint_task
            await self._close_http_client(team)
            # Queued messages this run took are deleted once the saved
            # conversation holds them (``DurableMessageQueue``).
            queue = getattr(self._backend, "message_queue", None)
            if isinstance(queue, DurableMessageQueue):
                await queue.settle(self._session.db)
            # Memory files the run wrote, whatever tool wrote them.
            mirror = getattr(self._session, "memory_mirror", None)
            if isinstance(mirror, MemoryMirror):
                await asyncio.to_thread(mirror.sync)

        # Fire Stop hook after the natural end-of-run.
        stop_gate = await self._hook_gate.fire_stop()
        if not stop_gate.should_continue and stop_gate.block_message:
            yield msg.Info(text=stop_gate.block_message)
        self._transition_to(RunPhase.done)
