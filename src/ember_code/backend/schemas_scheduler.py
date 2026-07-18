"""Typed hook payloads + chat view models for the scheduler layer.

Two groups of Pydantic models live here:

Hook payloads (BE → HookExecutor):

* :class:`TaskCreatedPayload` — sent from
  ``SchedulerController._TaskHookBridge.on_started`` when the runner
  picks a due task off the store and starts running it.
* :class:`TaskCompletedPayload` — sent from
  ``SchedulerController._TaskHookBridge.on_completed`` on either
  success or failure.

Both hook consumers call ``.model_dump()`` when handing the payload
to :meth:`HookExecutor.execute(payload=...)` so the wire dict shape
is unchanged from the pre-refactor raw-dict path — the win is that
the payload fields are now validated + documented in one place
instead of being spelled out at each call site.

Chat view models (BE → FE via ``/schedule`` slash command):

* :class:`TaskScheduledView` — successful ``add``: carries a
  :class:`CommandAction.SCHEDULE` action so the FE opens the panel.
  Branches on ``recurrence == ""`` for one-shot vs. recurring
  wording in ONE place.
* :class:`TaskCancelledView` — successful ``cancel``.
* :class:`TaskAlreadyDoneView` — cancel attempted on a
  completed/failed/cancelled task.
* :class:`TaskNotFoundView` — cancel/show on an unknown id.
* :class:`TaskDetailsView` — ``show <id>`` markdown block, including
  the optional Result/Error tail.
* :class:`ScheduleUsageView` — usage/help block for the parse-fail
  branch. Zero-field classmethod view (mirrors
  :class:`LoopUsageView`).

Every chat view exposes ``.to_command_result()`` — mirrors the
sibling :mod:`schemas_loop` pattern so the coordinator constructs a
view and returns its render, never a raw
:class:`CommandResult(...)` literal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction, CommandResultKind

if TYPE_CHECKING:
    from ember_code.core.scheduler.models import ScheduledTask, TaskStatus


class TaskCreatedPayload(BaseModel):
    """Fired when the scheduler picks a due task off the store and
    starts running it. Consumed by hook plugins that want to react
    to a new background task (e.g. a Slack notifier)."""

    session_id: str
    task_id: str
    description: str


class TaskCompletedPayload(BaseModel):
    """Fired when the scheduler finishes a task (success or failure).

    ``status`` is a two-value enum (``"completed"`` on success,
    ``"error"`` on either a timeout or an exception during
    execution) so hook consumers can branch on outcome without
    parsing free-form error text.
    """

    session_id: str
    task_id: str
    description: str
    status: Literal["completed", "error"]


# ── Chat view models ────────────────────────────────────────────


class TaskScheduledView(BaseModel):
    """Successful ``/schedule add`` — carries the ``SCHEDULE`` action.

    Branches on ``recurrence`` being empty for one-shot vs. recurring
    wording in a single ``to_command_result`` implementation so the
    coordinator has no per-shape branch.
    """

    task_id: str
    description: str
    scheduled_at: str  # already-formatted ``%Y-%m-%d %H:%M`` string
    recurrence: str = ""

    def to_command_result(self) -> CommandResult:
        if self.recurrence:
            content = (
                f'Scheduled `{self.task_id}`: "{self.description}" '
                f"({self.recurrence}, first at {self.scheduled_at})"
            )
        else:
            content = f'Scheduled `{self.task_id}`: "{self.description}" at {self.scheduled_at}'
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=content,
            action=CommandAction.SCHEDULE,
        )


class TaskCancelledView(BaseModel):
    """Successful ``/schedule cancel <id>``."""

    task_id: str

    def to_command_result(self) -> CommandResult:
        return CommandResult.info(f"Cancelled task {self.task_id}")


class TaskAlreadyDoneView(BaseModel):
    """``/schedule cancel <id>`` on a terminal-state task."""

    task_id: str
    status: TaskStatus

    def to_command_result(self) -> CommandResult:
        return CommandResult.info(f"Task {self.task_id} is already {self.status.value}")


class TaskNotFoundView(BaseModel):
    """``/schedule cancel``/``show`` on an unknown id."""

    task_id: str

    def to_command_result(self) -> CommandResult:
        return CommandResult.error(f"Task not found: {self.task_id}")


class TaskDetailsView(BaseModel):
    """Markdown block for ``/schedule show <id>``.

    Composes a whole :class:`ScheduledTask`. The render lives here
    (rather than as ``ScheduledTask.to_markdown()``) so the domain
    model stays view-free — matches the sibling ``schemas_loop.py``
    convention.
    """

    task: ScheduledTask

    model_config = {"arbitrary_types_allowed": True}

    def to_command_result(self) -> CommandResult:
        task = self.task
        lines = (
            f"## Task {task.id}\n"
            f"- **Description:** {task.description}\n"
            f"- **Scheduled:** {task.scheduled_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"- **Status:** {task.status.value}\n"
            f"- **Created:** {task.created_at.strftime('%Y-%m-%d %H:%M')}\n"
        )
        if task.result:
            lines += f"\n**Result:**\n{task.result}\n"
        if task.error:
            lines += f"\n**Error:**\n{task.error}\n"
        return CommandResult.markdown(lines)


class ScheduleUsageView(BaseModel):
    """``/schedule <phrase>`` where the phrase didn't parse.

    Zero-field classmethod view — mirrors :class:`LoopUsageView`.
    """

    @classmethod
    def to_command_result(cls) -> CommandResult:
        return CommandResult.error(
            "Could not parse the time clause. The `add` prefix is "
            "optional — any phrasing that contains `at`, `in`, `on`, "
            "`tomorrow`, `every`, `daily`, `hourly`, or `weekly` works.\n\n"
            "Examples:\n"
            "  /schedule review the codebase at 5pm\n"
            "  /schedule run tests in 30 minutes\n"
            "  /schedule audit security tomorrow\n"
            "  /schedule run tests every 2 hours\n"
            "  /schedule check dependencies daily"
        )


__all__ = [
    "TaskCreatedPayload",
    "TaskCompletedPayload",
    "TaskScheduledView",
    "TaskCancelledView",
    "TaskAlreadyDoneView",
    "TaskNotFoundView",
    "TaskDetailsView",
    "ScheduleUsageView",
]
