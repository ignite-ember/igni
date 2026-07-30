"""Pydantic DTOs and typed value objects for the monitor subsystem.

Keeping the value types in their own module mirrors the sibling
``core/plugins/models.py`` / ``core/scheduler/models.py`` /
``core/loop/models.py`` convention. Everything under
``ember_code.core.monitors`` that leaves the package as a value
lives here.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class MonitorStatus(str, Enum):
    """Lifecycle status of a single monitor.

    Str-Enum so the ``.value`` serialises cleanly onto the wire
    (Pydantic emits the string on ``.model_dump()`` / JSON) while
    the Python surface remains an enum — the supervisor's decision
    logic gets exhaustiveness-checkable identity comparisons instead
    of bare-string equality.
    """

    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"


class MonitorSnapshot(BaseModel):
    """Status-line summary for one monitor.

    ``pid`` and ``exit_code`` are nullable because a not-yet-started
    monitor has neither. ``status`` is typed with
    :class:`MonitorStatus` — Pydantic serialises to the ``.value``.
    """

    name: str
    command: str
    status: MonitorStatus
    pid: int | None
    uptime_seconds: float
    exit_code: int | None
    crash_count: int
    restart: str


# ── Restart-policy decision union ─────────────────────────────
#
# The supervisor's old if/elif over ``policy in ("never", "on_crash",
# "always")`` plus crash-count comparisons melts into a typed
# discriminated union. Each variant carries exactly the data the
# supervisor needs to act on — no more string dispatch.


class StopDecision(BaseModel):
    """The supervisor should mark the monitor stopped and exit."""

    action: Literal["stop"] = "stop"
    reason: str = ""


class BackoffDecision(BaseModel):
    """The supervisor should sleep ``delay_seconds`` then relaunch."""

    action: Literal["backoff"] = "backoff"
    delay_seconds: float
    attempt: int


class GiveUpDecision(BaseModel):
    """The supervisor exhausted its restart budget — mark failed."""

    action: Literal["give_up"] = "give_up"
    reason: str


RestartDecision = Annotated[
    StopDecision | BackoffDecision | GiveUpDecision,
    Field(discriminator="action"),
]


class MonitorControlResult(BaseModel):
    """Typed return value for :meth:`MonitorManager.restart` and
    :meth:`MonitorManager.stop`.

    Callers used to receive a bare string like ``"Restarted x."``;
    now they get a structured result they can branch on
    (``.ok`` / ``.action``) with the wire-facing string preserved
    on ``.reason``. ``__str__`` returns ``.reason`` so any caller
    still forwarding the value straight to a string sink keeps
    working.
    """

    ok: bool
    name: str
    action: Literal["restart", "stop"]
    reason: str

    def __str__(self) -> str:
        return self.reason


__all__ = [
    "BackoffDecision",
    "GiveUpDecision",
    "MonitorControlResult",
    "MonitorSnapshot",
    "MonitorStatus",
    "RestartDecision",
    "StopDecision",
]
