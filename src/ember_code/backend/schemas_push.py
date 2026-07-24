"""Typed payloads for every ``PushNotification`` the BE sends the FE.

Extracted out of :mod:`ember_code.backend.__main__` where dozens of
raw ``dict`` literals used to be constructed inline. Every push
payload flowing through :class:`PushNotificationBridge` is a Pydantic
model here so the wire shape can be validated at the seam.

Naming mirrors the ``PushNotification.channel`` value one-for-one so
the FE contract stays greppable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class LoginStatusPayload(BaseModel):
    """Channel ``login_status``. Human-readable status text streamed
    while a login attempt is in flight."""

    text: str


class LoginResultPayload(BaseModel):
    """Channel ``login_result``. Terminal result of a login attempt
    (success + reason string). Pushed after the RPC ack, once the
    Anthropic OAuth flow completes."""

    success: bool
    result: str


class SchedulerStartedPayload(BaseModel):
    """Channel ``scheduler_started``. The scheduled-task runner just
    picked up a task and started executing it."""

    task_id: str
    description: str


class SchedulerCompletedPayload(BaseModel):
    """Channel ``scheduler_completed``. Terminal result of a
    scheduled task's execution."""

    task_id: str
    description: str
    result: str


class BackgroundProcessDonePayload(BaseModel):
    """Channel ``background_process_done``. Best-effort ping so the
    UI can badge the watcher panel when a backgrounded shell command
    finishes. The queue-injected assistant nudge carries the tail
    output; this payload is a lightweight header."""

    pid: int | None
    cmd: str
    exit_code: int | None
    duration_seconds: float


class FileEditedPayload(BaseModel):
    """Channel ``file_edited``. Fired every time an edit tool writes
    to disk. JetBrains plugin listens for this to refresh the VFS
    (so Local History captures the change)."""

    path: str


class ProcessStartedPayload(BaseModel):
    """Channel ``process_started``. Emitted when the shell tool
    launches a new background process."""

    pid: int | None
    cmd: str | None
    started_at: float | None


class ProcessLinePayload(BaseModel):
    """Channel ``process_line``. One line of stdout/stderr from a
    running background process (streamed live to the watcher panel)."""

    pid: int | None
    line: str | None


class ProcessExitedPayload(BaseModel):
    """Channel ``process_exited``. A background process finished —
    the watcher panel flips its status pill."""

    pid: int | None
    cmd: str | None
    exit_code: int | None
    duration_seconds: float | None


class OrchestrateProgressLinePayload(BaseModel):
    """Channel ``orchestrate_progress``. Plain string progress line
    from the ``orchestrate`` tool — used only when the tool hasn't
    been ported to the structured-event form yet."""

    line: str


class SessionNamedPayload(BaseModel):
    """Channel ``session_named``. The BE picked an auto-generated
    display name for a freshly-created session."""

    session_id: str
    name: str


class ProcessDoneEvent(BaseModel):
    """Adaptor around the ``dict`` payload emitted by
    :attr:`core.tools.process_supervisor_locator.supervisors`
    (``supervisors.default().registry.bus``).

    The bus contract still hands us a raw dict (flipping the bus
    would touch a lot of subscribers) — we validate the shape at
    the ingress point (:class:`PushNotificationBridge`) instead.
    """

    pid: int | None = None
    cmd: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    output_tail: str = ""
    started_at: float | None = None
    line: str | None = None

    @classmethod
    def from_bus(cls, info: dict[str, Any]) -> ProcessDoneEvent:
        """Coerce whatever the bus subscriber received into a typed
        payload. Missing keys default to sane values so a partial
        subscriber implementation doesn't crash the bridge."""
        return cls(
            pid=info.get("pid"),
            cmd=str(info.get("cmd", "") or ""),
            exit_code=info.get("exit_code"),
            duration_seconds=float(info.get("duration_seconds", 0.0) or 0.0),
            output_tail=str(info.get("output_tail", "") or ""),
            started_at=info.get("started_at"),
            line=info.get("line"),
        )
