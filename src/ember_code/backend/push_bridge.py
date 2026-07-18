"""Cross-thread push-notification bridge.

The BE's tools + subscribers can fire from arbitrary threads: the
shell reader thread, the file-edit listener thread, the orchestrate
progress callback. ``transport.send`` is async and must run on the
event loop. :class:`PushNotificationBridge` centralises the
loop-affinity + coroutine-scheduling boilerplate that used to live
as five separate nested closures inside ``__main__._run``.

Every payload is a typed :mod:`schemas_push` model — no more raw
``dict`` literals flowing across the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ember_code.backend.schemas_push import (
    BackgroundProcessDonePayload,
    FileEditedPayload,
    LoginResultPayload,
    LoginStatusPayload,
    OrchestrateProgressLinePayload,
    ProcessDoneEvent,
    ProcessExitedPayload,
    ProcessLinePayload,
    ProcessStartedPayload,
    SchedulerCompletedPayload,
    SchedulerStartedPayload,
    SessionNamedPayload,
)
from ember_code.core.tools.edit import (
    FileEditNotifier,
    default_file_edit_notifier,
)
from ember_code.protocol import messages as msg

logger = logging.getLogger(__name__)


class PushNotificationBridge:
    """Wraps a transport + event loop, exposing every push channel
    as a bound method with a typed payload.

    Constructed once during BE boot. The transport it stamps
    through can be replaced per-runtime via :meth:`for_transport`
    (used by :class:`SessionOrchestrator` when a pooled runtime's
    ``SessionStampingTransport`` needs to carry its own session id
    on broadcasts).
    """

    def __init__(
        self,
        *,
        transport: Any,
        loop: asyncio.AbstractEventLoop | None,
        queue: list[str],
        file_edit_notifier: FileEditNotifier | None = None,
    ) -> None:
        self._transport = transport
        self._loop = loop
        self._queue = queue
        # Fall back to the module-level default so a bridge
        # constructed without an explicit notifier still binds to
        # the same instance ``EmberEditTools`` uses by default. This
        # preserves the shared-instance invariant (both sides
        # observe the same listener) with zero call-site churn.
        self._file_edit_notifier = file_edit_notifier or default_file_edit_notifier

    # ── Runtime binding ─────────────────────────────────────────

    def for_transport(self, transport: Any) -> PushNotificationBridge:
        """Sub-bridge bound to a different transport (typically a
        :class:`SessionStampingTransport` for a pooled runtime). The
        loop + queue + file-edit notifier references are shared with
        the parent bridge."""
        return PushNotificationBridge(
            transport=transport,
            loop=self._loop,
            queue=self._queue,
            file_edit_notifier=self._file_edit_notifier,
        )

    # ── Login channel ────────────────────────────────────────────

    async def on_login_status(self, text: str) -> None:
        await self._transport.send(msg.push_login_status(LoginStatusPayload(text=text)))

    async def on_login_result(self, success: bool, result: str) -> None:
        await self._transport.send(
            msg.push_login_result(LoginResultPayload(success=success, result=result))
        )

    # ── Scheduler channel ────────────────────────────────────────

    def _on_scheduler_started(self, task_id: str, description: str) -> None:
        payload = SchedulerStartedPayload(task_id=task_id, description=description)
        asyncio.ensure_future(self._transport.send(msg.push_scheduler_started(payload)))

    def _on_scheduler_completed(self, task_id: str, description: str, result: str) -> None:
        payload = SchedulerCompletedPayload(task_id=task_id, description=description, result=result)
        asyncio.ensure_future(self._transport.send(msg.push_scheduler_completed(payload)))

    def start_scheduler(self, backend: Any) -> None:
        """Kick off the scheduled-task poller with our push
        callbacks. Replaces the old free-function
        ``_start_scheduler_with_push``."""
        backend.start_scheduler(
            on_task_started=self._on_scheduler_started,
            on_task_completed=self._on_scheduler_completed,
        )

    # ── Background-process channel ──────────────────────────────

    def bind_to_process_supervisor(self) -> None:
        """Subscribe our per-line / start / exit handlers on the
        default process registry bus. Idempotent-safe: the bus
        uses an append-only subscriber list.

        Post-split (see ``process_supervisor.py`` module docstring)
        the bus lives on
        :class:`~ember_code.core.tools.process_registry.ProcessRegistry`
        rather than on the supervisor — event emission co-locates
        with the state that emits it.
        """
        from ember_code.core.tools.process_supervisor_locator import supervisors  # noqa: PLC0415

        bus = supervisors.default().registry.bus
        bus.on("start", self._on_process_started)
        bus.on("line", self._on_process_line)
        bus.on("exit", self._on_process_exited)
        bus.on("exit", self._on_process_done)

    def _on_process_started(self, info: dict) -> None:
        event = ProcessDoneEvent.from_bus(info)
        payload = ProcessStartedPayload(pid=event.pid, cmd=event.cmd, started_at=event.started_at)
        self._schedule_push("process_started", payload.model_dump())

    def _on_process_line(self, info: dict) -> None:
        event = ProcessDoneEvent.from_bus(info)
        payload = ProcessLinePayload(pid=event.pid, line=event.line)
        self._schedule_push("process_line", payload.model_dump())

    def _on_process_exited(self, info: dict) -> None:
        event = ProcessDoneEvent.from_bus(info)
        payload = ProcessExitedPayload(
            pid=event.pid,
            cmd=event.cmd,
            exit_code=event.exit_code,
            duration_seconds=event.duration_seconds,
        )
        self._schedule_push("process_exited", payload.model_dump())

    def _on_process_done(self, info: dict) -> None:
        """Long-form background-process completion notice + queue
        injection. The queue-injected text lands as a synthetic
        user message on the next tool result so the agent reacts to
        the completion naturally."""
        event = ProcessDoneEvent.from_bus(info)
        status = "succeeded" if event.exit_code == 0 else f"failed (exit {event.exit_code})"
        # Include a one-line hint pointing at ``read_process_output``
        # so the agent knows it can pull more than the 40-line tail.
        hint = f"For more output: read_process_output({event.pid}, tail=N)"
        if event.output_tail:
            msg_text = (
                f"BACKGROUND PROCESS COMPLETED\n"
                f"PID {event.pid}: {event.cmd}\n"
                f"Status: {status}  ·  Duration: {event.duration_seconds:.1f}s\n\n"
                f"Last output (tail):\n{event.output_tail}\n\n{hint}"
            )
        else:
            msg_text = (
                f"BACKGROUND PROCESS COMPLETED\n"
                f"PID {event.pid}: {event.cmd}\n"
                f"Status: {status}  ·  Duration: {event.duration_seconds:.1f}s\n\n{hint}"
            )
        self._queue.append(msg_text)
        payload = BackgroundProcessDonePayload(
            pid=event.pid,
            cmd=event.cmd,
            exit_code=event.exit_code,
            duration_seconds=event.duration_seconds,
        )
        self._schedule_push("background_process_done", payload.model_dump())

    # ── File-edit channel ───────────────────────────────────────

    def bind_to_file_edit_listener(self) -> None:
        """Register our path-forwarding callback with the shared
        :class:`FileEditNotifier`. The notifier reference was
        captured at construction time — see ``__init__``."""
        self._file_edit_notifier.set_listener(self._on_file_edited)

    def _on_file_edited(self, abs_path: str) -> None:
        payload = FileEditedPayload(path=abs_path)
        self._schedule_push("file_edited", payload.model_dump())

    # ── Broadcast bus ───────────────────────────────────────────

    def bind_to_broadcast_bus(self, backend: Any) -> None:
        """Wire the backend's broadcast bus to fan every broadcast
        out as a :class:`PushNotification` stamped for this bridge's
        transport. Pooled runtimes bind a bridge whose transport is
        their :class:`SessionStampingTransport` so ``session_id`` is
        auto-stamped on every push.

        Uses :meth:`BackendServer.register_broadcast` — no reach
        into ``backend._session.broadcast_bus`` — so the seam is
        typed at BackendServer, not scattered across coordinators.
        """
        if backend is None:
            return
        backend.register_broadcast(self._on_broadcast)

    def _on_broadcast(self, channel: str, payload: dict) -> None:
        """Sync-callable broadcast handler (session.broadcast is
        sync and may be called from a tool-call context that isn't
        itself awaiting). We hop onto the loop via
        ``call_soon_threadsafe``.

        The payload is already Pydantic-validated at the broadcast
        source (see :mod:`core.session.broadcast_schema`); we
        forward it as-is so downstream views see the exact same
        wire shape the broadcast defined."""
        self._schedule_push(channel, payload)

    # ── Orchestrate progress ────────────────────────────────────

    def on_orchestrate_progress(self, event: Any) -> None:
        """Handle both structured-event dicts (new orchestrate.py
        contract) and plain string progress lines (unported paths).
        Kept sync — the tool fires it from inside a run without
        awaiting."""
        if isinstance(event, dict):
            self._schedule_push("orchestrate_event", event)
        else:
            payload = OrchestrateProgressLinePayload(line=str(event))
            self._schedule_push("orchestrate_progress", payload.model_dump())

    # ── Session naming ──────────────────────────────────────────

    async def on_session_named(self, session_id: str, name: str) -> None:
        payload = SessionNamedPayload(session_id=session_id, name=name)
        await self._transport.send(msg.push_session_named(payload))

    # ── Composite wiring ────────────────────────────────────────

    def wire_all(self, backend: Any) -> None:
        """One call = every push channel bound to its source.
        Called by :class:`BackendApp` after backend + session are
        up. Individual ``bind_*`` methods stay public so tests can
        wire selectively."""
        self.bind_to_process_supervisor()
        self.bind_to_file_edit_listener()
        backend.wire_orchestrate_progress(self.on_orchestrate_progress)
        self.bind_to_broadcast_bus(backend)

    # ── Private helpers ─────────────────────────────────────────

    def _schedule_push(self, channel: str, payload: dict) -> None:
        """Hop onto the running loop and enqueue a
        :class:`PushNotification`. Safe to call from any thread —
        the reader thread + the file-edit listener thread + the
        orchestrate progress thread all end up here."""

        def _send() -> None:
            asyncio.ensure_future(
                self._transport.send(msg.PushNotification(channel=channel, payload=payload))
            )

        # Event loop closed during shutdown — drop the push instead
        # of crashing the callback thread. ``loop`` is optional so
        # test harnesses that only exercise sync RPC handlers can
        # construct the bridge without a running loop.
        if self._loop is None:
            return
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(_send)
