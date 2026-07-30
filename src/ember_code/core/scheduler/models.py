"""Scheduler data models."""

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from ember_code.core.scheduler.recurrence import Recurrence


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def is_active(self) -> bool:
        """Domain fact: ``pending`` and ``running`` are the not-done states.

        Kept as a pure enum concern (no view coupling) so backend code
        can call ``task.status.is_active`` alongside the TUI widgets.
        """
        return self in (TaskStatus.pending, TaskStatus.running)

    @property
    def terminal_icon(self) -> str:
        """Rich-markup icon for TUI rendering.

        Named ``terminal_icon`` (not ``icon``) because the value is
        Rich markup that is TUI-specific — VSCode/JetBrains frontends
        must map from the enum on their own side.  Adding a new
        :class:`TaskStatus` member without extending this property
        will raise :class:`KeyError` at render time, which is the
        desired fail-loud behaviour (versus the old ``.get(..., "?")``
        dict lookup that silently rendered ``?``).
        """
        return _TERMINAL_ICONS[self]


# Presentation lookup lives beside the enum so ``TaskStatus.terminal_icon``
# is the single source of truth for TUI status icons — replaces the
# duplicated ``_STATUS_ICONS`` dicts previously scattered across the
# widget layer.
_TERMINAL_ICONS: dict["TaskStatus", str] = {
    TaskStatus.pending: "[dim]⏳[/dim]",
    TaskStatus.running: "[bold yellow]⚡[/bold yellow]",
    TaskStatus.completed: "[green]✓[/green]",
    TaskStatus.failed: "[red]✗[/red]",
    TaskStatus.cancelled: "[dim]—[/dim]",
}


class ScheduledTask(BaseModel):
    """A task scheduled for deferred execution.

    For recurring tasks, ``recurrence`` holds the repeat pattern (e.g.
    "every 1 hours", "daily", "weekly"). After completion, the runner
    creates the next occurrence automatically.
    """

    id: str
    description: str
    scheduled_at: datetime
    created_at: datetime = Field(default_factory=datetime.now)
    status: TaskStatus = TaskStatus.pending
    result: str = ""
    error: str = ""
    recurrence: str = ""  # empty = one-shot, otherwise repeat pattern

    @classmethod
    def new(
        cls,
        description: str,
        scheduled_at: datetime,
        recurrence: str = "",
    ) -> "ScheduledTask":
        """Construct a fresh task with a generated 8-hex-char id.

        Owns the id-generation invariant so callers stop duplicating
        ``uuid.uuid4().hex[:8]`` across five sites (the backend
        ``/schedule`` coordinator, the agent-facing
        :class:`~ember_code.core.tools.schedule.ScheduleTools`
        toolkit, and the recurring-task rescheduler in
        :class:`~ember_code.core.scheduler.runner.SchedulerRunner`).
        Same "behaviour on the model" pattern as the
        :meth:`TaskStatus.terminal_icon` / :meth:`TaskStatus.is_active`
        properties above.
        """
        return cls(
            id=uuid.uuid4().hex[:8],
            description=description,
            scheduled_at=scheduled_at,
            recurrence=recurrence,
        )

    @field_validator("recurrence")
    @classmethod
    def _recurrence_must_be_canonical(cls, v: str) -> str:
        """Fail loudly if the recurrence string isn't the canonical form.

        The DB column stores a plain string for wire-compatibility, but
        every non-empty value must round-trip through
        :meth:`Recurrence.from_canonical` — otherwise ``recurrence_obj``
        would silently return ``None`` and the rescheduler would drop
        the recurring task without an error at construction time.
        """
        if v and Recurrence.from_canonical(v) is None:
            raise ValueError(f"recurrence must be canonical (e.g. 'every 1 days'), got {v!r}")
        return v

    @property
    def recurrence_obj(self) -> Recurrence | None:
        """Typed view of the persisted recurrence string.

        Returns ``None`` for one-shot tasks (``recurrence == ""``) so
        callers can guard with a single ``if task.recurrence_obj``
        check instead of re-parsing the canonical string at every use
        site. Replaces the old ``next_occurrence_from_recurrence``
        free-function — the operation now lives on the value object.
        """
        return Recurrence.from_canonical(self.recurrence)
