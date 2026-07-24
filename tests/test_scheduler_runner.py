"""Tests for scheduler/runner.py — background task execution."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.runner import SchedulerRunner


class TestSchedulerRunner:
    def _make_runner(self, store=None, execute_fn=None, **kwargs):
        store = store or MagicMock()
        execute_fn = execute_fn or AsyncMock(return_value="done")
        defaults = dict(
            store=store,
            execute_fn=execute_fn,
            poll_interval=0.1,
            task_timeout=5,
            max_concurrent=1,
        )
        defaults.update(kwargs)
        return SchedulerRunner(**defaults)

    @pytest.mark.asyncio
    async def test_starts_and_stops(self):
        runner = self._make_runner()
        runner.start()
        assert runner.is_running
        runner.stop()
        assert not runner.is_running

    @pytest.mark.asyncio
    async def test_double_start_is_safe(self):
        runner = self._make_runner()
        runner.start()
        runner.start()  # should not raise
        assert runner.is_running
        runner.stop()

    def test_stop_without_start(self):
        runner = self._make_runner()
        runner.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_executes_due_task(self):
        task = ScheduledTask(
            id="t1",
            description="test task",
            scheduled_at=datetime(2020, 1, 1),
            status=TaskStatus.pending,
        )

        store = MagicMock()
        store.get_due_tasks = AsyncMock(return_value=[task])
        store.update_status = AsyncMock()
        store.get = AsyncMock(return_value=None)  # for _reschedule_if_recurring

        execute_fn = AsyncMock(return_value="task result")

        runner = SchedulerRunner(
            store=store,
            execute_fn=execute_fn,
            poll_interval=0.1,
            task_timeout=5,
            max_concurrent=1,
        )

        await runner._check_and_spawn()
        # Wait for the spawned task to complete
        await asyncio.sleep(0.5)

        execute_fn.assert_called_once_with("test task")

    @pytest.mark.asyncio
    async def test_respects_max_concurrent(self):
        runner = self._make_runner(max_concurrent=2)
        assert runner._semaphore._value == 2

    @pytest.mark.asyncio
    async def test_callbacks_called(self):
        task = ScheduledTask(
            id="t2",
            description="callback test",
            scheduled_at=datetime(2020, 1, 1),
            status=TaskStatus.pending,
        )

        store = MagicMock()
        store.get_due_tasks = AsyncMock(return_value=[task])
        store.update_status = AsyncMock()
        store.get = AsyncMock(return_value=None)  # for _reschedule_if_recurring

        on_started = MagicMock()
        on_completed = MagicMock()

        runner = SchedulerRunner(
            store=store,
            execute_fn=AsyncMock(return_value="ok"),
            on_task_started=on_started,
            on_task_completed=on_completed,
            poll_interval=0.1,
            task_timeout=5,
            max_concurrent=1,
        )

        await runner._check_and_spawn()
        await asyncio.sleep(0.5)

        on_started.assert_called_once_with("t2", "callback test")
        on_completed.assert_called_once_with("t2", "callback test", True)
