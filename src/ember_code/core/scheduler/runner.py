"""Background scheduler runner — polls for due tasks and executes them.

Tasks are executed with bounded concurrency via an asyncio semaphore.
By default only one task runs at a time (sequential), but this is
configurable. Each task has a timeout to prevent runaway executions.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.parser import next_occurrence_from_recurrence
from ember_code.core.scheduler.store import TaskStore

logger = logging.getLogger(__name__)


class SchedulerRunner:
    """Polls the task store and executes due tasks with bounded concurrency.

    Parameters
    ----------
    store:
        The task store to poll.
    execute_fn:
        Async callback ``(task_description: str) -> str`` that runs the task
        through the AI agent and returns the result text.
    on_task_started:
        Optional callback when a task begins executing.
    on_task_completed:
        Optional callback when a task finishes (success or failure).
    poll_interval:
        Seconds between polls. Default 30.
    task_timeout:
        Maximum seconds a single task may run before being cancelled.
        Default 300 (5 minutes).
    max_concurrent:
        Maximum number of tasks executing at the same time. Default 1
        (sequential execution).
    """

    def __init__(
        self,
        store: TaskStore,
        execute_fn: Callable[[str], Coroutine[Any, Any, str]],
        on_task_started: Callable[[str, str], Any] | None = None,
        on_task_completed: Callable[[str, str, bool], Any] | None = None,
        poll_interval: float = 30,
        task_timeout: float = 300,
        max_concurrent: int = 1,
    ):
        self._store = store
        self._execute_fn = execute_fn
        self._on_task_started = on_task_started
        self._on_task_completed = on_task_completed
        self._poll_interval = poll_interval
        self._task_timeout = task_timeout
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._active_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Scheduler started (poll every %.0fs, timeout %.0fs, max concurrent %d)",
            self._poll_interval,
            self._task_timeout,
            self._semaphore._value,
        )

    def stop(self) -> None:
        """Stop the polling loop and cancel active task executions."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        for t in self._active_tasks:
            t.cancel()
        self._active_tasks.clear()
        logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._check_and_spawn()
            except Exception:
                logger.exception("Scheduler poll error")
            await asyncio.sleep(self._poll_interval)

    async def _check_and_spawn(self) -> None:
        """Find due tasks and spawn them, respecting the concurrency limit."""
        due_tasks = await self._store.get_due_tasks()
        for task in due_tasks:
            # Mark as running so the next poll doesn't pick it up again
            await self._store.update_status(task.id, TaskStatus.running)
            logger.info("Spawning scheduled task %s: %s", task.id, task.description)

            bg = asyncio.create_task(self._run_with_semaphore(task.id, task.description))
            self._active_tasks.add(bg)
            bg.add_done_callback(self._active_tasks.discard)

    async def _run_with_semaphore(self, task_id: str, description: str) -> None:
        """Acquire the semaphore, then execute the task."""
        async with self._semaphore:
            await self._execute_task(task_id, description)

    async def _execute_task(self, task_id: str, description: str) -> None:
        """Execute a single task with a timeout.

        If the task has a recurrence pattern, a new pending task is created
        for the next occurrence after completion (success or failure).
        """
        if self._on_task_started:
            self._on_task_started(task_id, description)

        try:
            result = await asyncio.wait_for(
                self._execute_fn(description),
                timeout=self._task_timeout,
            )
            await self._store.update_status(task_id, TaskStatus.completed, result=result)
            logger.info("Task %s completed", task_id)
            if self._on_task_completed:
                self._on_task_completed(task_id, description, True)
        except asyncio.TimeoutError:
            error_msg = f"Task timed out after {self._task_timeout:.0f}s"
            await self._store.update_status(task_id, TaskStatus.failed, error=error_msg)
            logger.error("Task %s timed out after %.0fs", task_id, self._task_timeout)
            if self._on_task_completed:
                self._on_task_completed(task_id, description, False)
        except Exception as e:
            error_msg = str(e)
            await self._store.update_status(task_id, TaskStatus.failed, error=error_msg)
            logger.error("Task %s failed: %s", task_id, error_msg)
            if self._on_task_completed:
                self._on_task_completed(task_id, description, False)

        # Reschedule recurring tasks (even after failure — the schedule continues)
        await self._reschedule_if_recurring(task_id)

    async def _reschedule_if_recurring(self, task_id: str) -> None:
        """If the task has a recurrence pattern, create the next occurrence."""
        task = await self._store.get(task_id)
        if not task or not task.recurrence:
            return

        next_at = next_occurrence_from_recurrence(task.recurrence, task.scheduled_at)
        if next_at is None:
            logger.warning("Could not compute next occurrence for task %s", task_id)
            return

        next_task = ScheduledTask(
            id=uuid.uuid4().hex[:8],
            description=task.description,
            scheduled_at=next_at,
            recurrence=task.recurrence,
        )
        await self._store.add(next_task)
        logger.info(
            "Rescheduled recurring task %s → %s at %s",
            task_id,
            next_task.id,
            next_at.strftime("%Y-%m-%d %H:%M"),
        )
