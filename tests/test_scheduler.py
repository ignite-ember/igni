"""Tests for the scheduler module."""

from datetime import datetime, timedelta

import pytest

from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.parser import (
    next_occurrence_from_recurrence,
    parse_recurrence,
    parse_time,
)
from ember_code.core.scheduler.store import TaskStore

# ── Time parser ─────────────────────────────────────────────────


class TestParseTime:
    def test_in_minutes(self):
        result = parse_time("in 5 minutes")
        assert result is not None
        assert result > datetime.now()
        assert result < datetime.now() + timedelta(minutes=6)

    def test_in_hours(self):
        result = parse_time("in 2 hours")
        assert result is not None
        assert result > datetime.now() + timedelta(hours=1, minutes=59)

    def test_in_days(self):
        result = parse_time("in 1 day")
        assert result is not None
        assert result > datetime.now() + timedelta(hours=23)

    def test_at_time(self):
        result = parse_time("at 14:00")
        assert result is not None
        assert result.hour == 14
        assert result.minute == 0

    def test_at_time_pm(self):
        result = parse_time("at 5pm")
        assert result is not None
        assert result.hour == 17

    def test_at_time_am(self):
        result = parse_time("at 9:30am")
        assert result is not None
        assert result.hour == 9
        assert result.minute == 30

    def test_tomorrow(self):
        result = parse_time("tomorrow")
        assert result is not None
        assert result.date() == (datetime.now() + timedelta(days=1)).date()

    def test_tomorrow_at(self):
        result = parse_time("tomorrow at 3pm")
        assert result is not None
        assert result.date() == (datetime.now() + timedelta(days=1)).date()
        assert result.hour == 15

    def test_iso_format(self):
        result = parse_time("2026-12-25 14:00")
        assert result is not None
        assert result.year == 2026
        assert result.month == 12
        assert result.hour == 14

    def test_invalid(self):
        assert parse_time("not a time") is None
        assert parse_time("") is None


# ── Recurrence parser ───────────────────────────────────────────


class TestParseRecurrence:
    def test_every_minutes(self):
        recurrence, scheduled = parse_recurrence("every 30 minutes")
        assert recurrence == "every 30 minutes"
        assert scheduled is not None
        assert scheduled > datetime.now()

    def test_every_hours(self):
        recurrence, scheduled = parse_recurrence("every 2 hours")
        assert recurrence == "every 2 hours"
        assert scheduled is not None

    def test_daily(self):
        recurrence, scheduled = parse_recurrence("daily")
        assert recurrence == "every 1 days"
        assert scheduled is not None

    def test_hourly(self):
        recurrence, scheduled = parse_recurrence("hourly")
        assert recurrence == "every 1 hours"
        assert scheduled is not None

    def test_weekly(self):
        recurrence, scheduled = parse_recurrence("weekly")
        assert recurrence == "every 7 days"
        assert scheduled is not None

    def test_daily_at_time(self):
        recurrence, scheduled = parse_recurrence("daily at 9am")
        assert recurrence == "every 1 days"
        assert scheduled is not None
        assert scheduled.hour == 9

    def test_invalid(self):
        recurrence, scheduled = parse_recurrence("not a pattern")
        assert recurrence == ""
        assert scheduled is None


class TestNextOccurrence:
    def test_from_daily(self):
        last = datetime(2026, 3, 19, 9, 0)
        next_at = next_occurrence_from_recurrence("every 1 days", last)
        assert next_at == datetime(2026, 3, 20, 9, 0)

    def test_from_hourly(self):
        last = datetime(2026, 3, 19, 14, 0)
        next_at = next_occurrence_from_recurrence("every 1 hours", last)
        assert next_at == datetime(2026, 3, 19, 15, 0)

    def test_from_30_minutes(self):
        last = datetime(2026, 3, 19, 14, 30)
        next_at = next_occurrence_from_recurrence("every 30 minutes", last)
        assert next_at == datetime(2026, 3, 19, 15, 0)

    def test_invalid_pattern(self):
        assert next_occurrence_from_recurrence("garbage", datetime.now()) is None


# ── Task store (async) ──────────────────────────────────────────


class TestTaskStore:
    """Each test gets its own SQLite tmp file. No cross-test interference."""

    @pytest.fixture
    def store(self, tmp_path):
        return TaskStore(db_path=tmp_path / "state.db")

    def _make_task(self, **kwargs):
        defaults = {
            "id": "test123",
            "description": "Run tests",
            "scheduled_at": datetime.now() + timedelta(hours=1),
        }
        defaults.update(kwargs)
        return ScheduledTask(**defaults)

    @pytest.mark.asyncio
    async def test_add_and_get(self, store):
        task = self._make_task()
        await store.add(task)
        retrieved = await store.get("test123")
        assert retrieved is not None
        assert retrieved.description == "Run tests"
        assert retrieved.status == TaskStatus.pending

    @pytest.mark.asyncio
    async def test_get_missing(self, store):
        assert await store.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_update_status(self, store):
        await store.add(self._make_task())
        await store.update_status("test123", TaskStatus.completed, result="All passed")
        task = await store.get("test123")
        assert task.status == TaskStatus.completed
        assert task.result == "All passed"

    @pytest.mark.asyncio
    async def test_remove(self, store):
        await store.add(self._make_task())
        assert await store.remove("test123") is True
        assert await store.get("test123") is None
        assert await store.remove("test123") is False

    @pytest.mark.asyncio
    async def test_get_due_tasks(self, store):
        await store.add(
            self._make_task(id="past", scheduled_at=datetime.now() - timedelta(minutes=1))
        )
        await store.add(
            self._make_task(id="future", scheduled_at=datetime.now() + timedelta(hours=1))
        )

        due = await store.get_due_tasks()
        ids = [t.id for t in due]
        assert ids == ["past"]

    @pytest.mark.asyncio
    async def test_get_all_excludes_done(self, store):
        await store.add(self._make_task(id="a"))
        await store.add(self._make_task(id="b"))
        await store.update_status("b", TaskStatus.completed)

        active = await store.get_all(include_done=False)
        assert [t.id for t in active] == ["a"]

        all_tasks = await store.get_all(include_done=True)
        assert {t.id for t in all_tasks} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_get_all_orders_by_scheduled_at_desc(self, store):
        """Tasks scheduled furthest in the future surface first.

        Both the TUI tasks panel and ``list_scheduled_tasks`` render
        the store's order verbatim — flipping the underlying ORDER BY
        here is the only place this contract is enforced, so a
        regression that drops ``.desc()`` (the historical default was
        ``scheduled_at`` ascending) would silently flip the user's
        view back to next-to-run-first.
        """
        now = datetime.now()
        await store.add(self._make_task(id="soonest", scheduled_at=now + timedelta(hours=1)))
        await store.add(self._make_task(id="latest", scheduled_at=now + timedelta(hours=3)))
        await store.add(self._make_task(id="middle", scheduled_at=now + timedelta(hours=2)))

        tasks = await store.get_all(include_done=False)
        assert [t.id for t in tasks] == ["latest", "middle", "soonest"]

    @pytest.mark.asyncio
    async def test_recurrence_persisted(self, store):
        task = self._make_task(recurrence="every 1 days")
        await store.add(task)
        retrieved = await store.get("test123")
        assert retrieved.recurrence == "every 1 days"

    @pytest.mark.asyncio
    async def test_recurrence_empty_by_default(self, store):
        await store.add(self._make_task())
        retrieved = await store.get("test123")
        assert retrieved.recurrence == ""
