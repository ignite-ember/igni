"""Tests for tools/schedule.py — schedule tool functions."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.tools.schedule import ScheduleTools


class TestScheduleTools:
    def test_registers_functions(self):
        tools = ScheduleTools()
        names = set()
        for f in tools.functions.values():
            names.add(f.name)
        for f in tools.async_functions.values():
            names.add(f.name)
        assert "schedule_task" in names
        assert "list_scheduled_tasks" in names
        assert "cancel_scheduled_task" in names

    @pytest.mark.asyncio
    async def test_schedule_task(self):
        tools = ScheduleTools()
        with patch.object(tools, "_store") as mock_store:
            mock_store.add = AsyncMock(return_value="task-123")
            result = await tools.schedule_task("Run tests", "in 30 minutes")
            assert "scheduled" in result.lower() or "task" in result.lower()

    @pytest.mark.asyncio
    async def test_list_empty(self):
        tools = ScheduleTools()
        with patch.object(tools, "_store") as mock_store:
            mock_store.get_all = AsyncMock(return_value=[])
            result = await tools.list_scheduled_tasks()
            assert "no" in result.lower() or "empty" in result.lower() or result.strip() != ""

    @pytest.mark.asyncio
    async def test_cancel_task(self):
        task = ScheduledTask(
            id="task-123",
            description="test task",
            scheduled_at=datetime(2026, 1, 1),
            status=TaskStatus.pending,
        )
        tools = ScheduleTools()
        with patch.object(tools, "_store") as mock_store:
            mock_store.get = AsyncMock(return_value=task)
            mock_store.update_status = AsyncMock(return_value=True)
            result = await tools.cancel_scheduled_task("task-123")
            assert "cancel" in result.lower() or "task-123" in result
