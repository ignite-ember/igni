"""Background scheduler runner — polls for due tasks and executes them.

Tasks are executed with bounded concurrency via an asyncio semaphore.
By default only one task runs at a time (sequential), but this is
configurable. Each task has a timeout to prevent runaway executions.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from ember_code.core.activity import ActivityRegistry, BusyKind, background
from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
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
        Optional callback when a task finishes (success or failure). Receives
        ``(task_id, description, succeeded, detail)`` — ``detail`` is the task's
        result text on success and the error message on failure. It is passed
        because the consumer publishes it: the push payload has a ``result: str``
        field, and sending the flag in its place raised ``ValidationError`` on
        every completion, so the notification never arrived.
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
        on_task_completed: Callable[[str, str, bool, str], Any] | None = None,
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
        self._wake_holder = f"scheduler:{id(self):x}"
        self._next_due: datetime | None = None

    def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._poll_task = background.daemon(self._poll_loop(), name="scheduler-poll")
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
        ActivityRegistry.shared().wake(self._wake_holder, None)
        logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _poll_loop(self) -> None:
        poked = asyncio.Event()
        loop = asyncio.get_running_loop()

        def poke() -> None:
            loop.call_soon_threadsafe(poked.set)

        registry = ActivityRegistry.shared()
        registry.on_rearm(poke)
        try:
            while self._running:
                try:
                    await self._check_and_spawn()
                except Exception:
                    logger.exception("Scheduler poll error")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(poked.wait(), self._next_delay())
                poked.clear()
        finally:
            registry.off_rearm(poke)

    #: How long the loop sleeps with nothing pending, unless poked: a task
    #: added in this process pokes it (``TaskStore.add``), and so does a
    #: restore (``ActivityRegistry.rearm``). Waking every ``poll_interval``
    #: with nothing to do would only dirty an idle process's memory.
    IDLE_POLL_SECONDS = 300.0

    def _next_delay(self) -> float:
        """Until the next task is due by the wall clock, at most ``poll_interval``."""
        if self._next_due is None:
            return max(self._poll_interval, self.IDLE_POLL_SECONDS)
        until = (self._next_due - datetime.now(UTC)).total_seconds()
        return min(self._poll_interval, max(0.0, until))

    async def _check_and_spawn(self) -> None:
        """Find due tasks and spawn them, respecting the concurrency limit."""
        due_tasks = await self._store.get_due_tasks()
        await self._publish_wake()
        for task in due_tasks:
            # Mark as running so the next poll doesn't pick it up again
            await self._store.update_status(task.id, TaskStatus.running)
            logger.info("Spawning scheduled task %s: %s", task.id, task.description)

            bg = background.start(
                self._run_with_semaphore(task.id, task.description),
                holder=f"scheduled-task:{task.id}",
                reason=task.description[:80],
                kind=BusyKind.SCHEDULED_TASK,
            )
            self._active_tasks.add(bg)
            bg.add_done_callback(self._active_tasks.discard)

    async def _publish_wake(self) -> None:
        """Tell the activity registry when the next task is due.

        So a process that is suspended while nothing runs is woken for
        it (``core/activity``). Times are stored as local wall-clock
        times; the wake is in UTC.
        """
        try:
            due = await self._store.next_pending_at()
        except Exception:
            logger.debug("could not read the next scheduled task", exc_info=True)
            return
        at = due.astimezone(UTC) if due is not None else None
        self._next_due = at
        ActivityRegistry.shared().wake(self._wake_holder, at, "a scheduled task")

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

        # ``unless_cancelled`` on every terminal write: a cancel issued
        # while the task was running used to be overwritten here, so the
        # user was told the recurring schedule had stopped and the next
        # occurrence was created anyway.
        try:
            result = await asyncio.wait_for(
                self._execute_fn(description),
                timeout=self._task_timeout,
            )
            succeeded, detail = True, str(result)
            written = await self._store.update_status(
                task_id, TaskStatus.completed, result=result, unless_cancelled=True
            )
            logger.info("Task %s completed", task_id)
        except asyncio.TimeoutError:
            succeeded, detail = False, f"Task timed out after {self._task_timeout:.0f}s"
            written = await self._store.update_status(
                task_id, TaskStatus.failed, error=detail, unless_cancelled=True
            )
            logger.error("Task %s timed out after %.0fs", task_id, self._task_timeout)
        except Exception as e:
            succeeded, detail = False, str(e)
            written = await self._store.update_status(
                task_id, TaskStatus.failed, error=detail, unless_cancelled=True
            )
            logger.error("Task %s failed: %s", task_id, detail)

        if written is False:
            # The terminal write lost to a cancel. Neither notifying a
            # completion nor rescheduling is honest here — the user was
            # told the schedule had stopped.
            logger.info("Task %s was cancelled while running — not rescheduling", task_id)
            return

        if self._on_task_completed:
            self._on_task_completed(task_id, description, succeeded, detail)

        # Reschedule recurring tasks (even after failure — the schedule continues)
        await self._reschedule_if_recurring(task_id)

    async def _reschedule_if_recurring(self, task_id: str) -> None:
        """If the task has a recurrence pattern, create the next occurrence."""
        task = await self._store.get(task_id)
        if not task or not task.recurrence:
            return
        if task.status == TaskStatus.cancelled:
            logger.info("Task %s cancelled — recurrence stopped", task_id)
            return

        recurrence = task.recurrence_obj
        if recurrence is None:
            logger.warning("Could not compute next occurrence for task %s", task_id)
            return
        # ``not_before=now`` collapses the occurrences that elapsed while
        # igni was closed. Anchoring purely on ``scheduled_at`` meant a
        # weekend offline with an hourly task came back as ~48 immediately
        # due occurrences, each replayed as a full unattended agent turn
        # with the main toolset. One catch-up run, then back on phase.
        next_at = recurrence.next_after(task.scheduled_at, not_before=datetime.now())

        next_task = ScheduledTask.new(
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
