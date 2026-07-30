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
from ember_code.core.tools.process_supervisor_locator import supervisors
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
        """
        if supervisors.default().cancel_foreground():
            logger.info("Killed foreground process on cancel")

        try:
            team = self._session.main_team
            run_id = getattr(team, "run_id", None)
            if run_id:
                Agent.cancel_run(run_id)
        except Exception as exc:
            logger.debug("Failed to cancel run: %s", exc)

        task = self._current_run_task
        if task and not task.done():
            logger.info("Cancelling run task %s", task.get_name())
            task.cancel()

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
        interrupted_summary_text = (
            self._interrupted_summary.summary_text
            if self._interrupted_summary is not None
            else None
        )
        self._interrupted_summary = None
        await self._pending_journal.drain_queued_drops()

        # Pre-run prompt assembly: mentions, media, URL, learnings,
        # timestamp + interrupted-run note.
        build = await self._prompt_builder.build(
            text, media, interrupted_summary=interrupted_summary_text
        )
        for info in build.info_messages:
            yield info
        message = build.message
        media = build.media

        # Fire UserPromptSubmit hook. Blocks the run when
        # should_continue=False; otherwise optionally appends a
        # <hook-context> block onto the prompt.
        gate = await self._hook_gate.fire_user_prompt_submit(text)
        if not gate.should_continue:
            yield msg.Error(text=gate.block_message or "Message blocked by hook.")
            self._transition_to(RunPhase.done)
            return
        if gate.context_message:
            message = f"{message}\n<hook-context>{gate.context_message}</hook-context>"

        # Pre-persist the user message so a kill mid-stream doesn't
        # lose it. On success the row is marked completed; on crash
        # it stays pending and the next --continue boot surfaces it.
        pending_id = await self._pending_journal.record(text)

        # Periodic checkpoint task — see SessionCheckpointer.run_forever
        # (BackendServer._periodic_checkpoint is the late-binding hook).
        checkpoint_task = asyncio.create_task(self._periodic_checkpoint_via_backend(team))

        media_kwargs = media.to_kwargs() if media is not None else {}
        self._transition_to(RunPhase.streaming)
        try:
            async for proto in self._stream_via_backend(
                team.arun(message, stream=True, **media_kwargs)
            ):
                yield proto
                await self._dispatcher.handle(proto, team)
            # Natural end-of-run — mark the pre-persisted user
            # message as completed.
            await self._pending_journal.mark_completed(pending_id)
            self._transition_to(RunPhase.finalizing)
        except Exception:
            self._transition_to(RunPhase.errored)
            raise
        finally:
            checkpoint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await checkpoint_task
            await self._close_http_client(team)

        # Fire Stop hook after the natural end-of-run.
        stop_gate = await self._hook_gate.fire_stop()
        if not stop_gate.should_continue and stop_gate.block_message:
            yield msg.Info(text=stop_gate.block_message)
        self._transition_to(RunPhase.done)
