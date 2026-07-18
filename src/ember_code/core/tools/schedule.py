"""Schedule tools — lets the AI agent create, list, and cancel scheduled tasks."""

from agno.tools import Toolkit

from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.parser import parse_time
from ember_code.core.scheduler.recurrence import Recurrence
from ember_code.core.scheduler.store import TaskStore


class ScheduleTools(Toolkit):
    """Toolkit for managing scheduled tasks from within agent conversations."""

    def __init__(self, store: TaskStore | None = None, project_dir: str | None = None, **kwargs):
        super().__init__(name="schedule_tools", **kwargs)
        # Lazy: ``TaskStore()`` runs alembic migrations against the
        # per-project state.db on construction. Defer that until the
        # first tool call so cheap toolkit-creation paths (mocks, agent
        # registration) don't open a database.
        self._store: TaskStore | None = store
        # Without this the store falls back to Path.cwd(), which is the
        # BE process cwd — not the session's project — in ws mode.
        self._project_dir = project_dir
        self.register(self.schedule_task)
        self.register(self.list_scheduled_tasks)
        self.register(self.cancel_scheduled_task)

    def _ensure_store(self) -> TaskStore:
        if self._store is None:
            self._store = TaskStore(project_dir=self._project_dir)
        return self._store

    async def schedule_task(self, description: str, when: str) -> str:
        """Schedule a task for deferred or recurring execution.

        Args:
            description: What the task should do (e.g., "Review the codebase for security issues").
            when: When to run it. Supports:
                  One-shot: "in 5 minutes", "at 5pm", "tomorrow", "2026-03-20 14:00"
                  Recurring: "every 30 minutes", "daily", "daily at 9am",
                             "hourly", "weekly", "every 2 hours"

        Returns:
            Confirmation message with the task ID and scheduled time.
        """
        # Try recurring first
        result = Recurrence.parse(when)
        if result is not None:
            canonical = result.recurrence.canonical()
            scheduled_at = result.first_scheduled
            task = ScheduledTask.new(
                description=description,
                scheduled_at=scheduled_at,
                recurrence=canonical,
            )
            await self._ensure_store().add(task)
            return (
                f'Scheduled recurring task `{task.id}`: "{description}" '
                f"({canonical}, first run at {scheduled_at.strftime('%Y-%m-%d %H:%M')})."
            )

        # One-shot
        scheduled_at = parse_time(when)
        if scheduled_at is None:
            return (
                f"Could not parse time: '{when}'. Try:\n"
                "  One-shot: 'in 30 minutes', 'at 5pm', 'tomorrow'\n"
                "  Recurring: 'daily', 'every 2 hours', 'weekly at 9am'"
            )

        task = ScheduledTask.new(
            description=description,
            scheduled_at=scheduled_at,
        )
        await self._ensure_store().add(task)
        return f'Scheduled task `{task.id}`: "{description}" at {scheduled_at.strftime("%Y-%m-%d %H:%M")}.'

    async def list_scheduled_tasks(self, include_done: bool = False) -> str:
        """List scheduled tasks.

        Args:
            include_done: If True, include completed/failed/cancelled tasks.

        Returns:
            Formatted list of tasks.
        """
        tasks = await self._ensure_store().get_all(include_done=include_done)
        if not tasks:
            return "No scheduled tasks."

        lines = []
        for t in tasks:
            time_str = t.scheduled_at.strftime("%Y-%m-%d %H:%M")
            recur = f" ({t.recurrence})" if t.recurrence else ""
            lines.append(f"- [{t.id}] {t.status.value} | {time_str}{recur} | {t.description}")
        return "\n".join(lines)

    async def cancel_scheduled_task(self, task_id: str) -> str:
        """Cancel a scheduled task (stops recurring tasks too).

        Args:
            task_id: The task ID to cancel.

        Returns:
            Confirmation or error message.
        """
        task = await self._ensure_store().get(task_id)
        if not task:
            return f"Task not found: {task_id}"
        if task.status not in (TaskStatus.pending, TaskStatus.running):
            return f"Task {task_id} is already {task.status.value}."
        await self._ensure_store().update_status(task_id, TaskStatus.cancelled)
        recur_note = " (recurring schedule stopped)" if task.recurrence else ""
        return f'Cancelled task {task_id}: "{task.description}"{recur_note}.'
