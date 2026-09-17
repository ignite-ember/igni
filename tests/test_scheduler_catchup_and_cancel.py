"""Scheduler: missed-occurrence catch-up, cancel-during-execution, legacy rows.

Covers SWP-38 (every occurrence missed while igni was closed was
replayed as a full unattended agent run), SWP-39 (a cancel issued while
the task was running was overwritten and the next occurrence created
anyway) and SWP-55 (a pre-validator ``recurrence`` row raised
``ValidationError`` out of ``get_due_tasks`` and killed the poll loop).
"""

from datetime import datetime, timedelta

import pytest

from ember_code.core.scheduler.db_models import ScheduledTaskModel
from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.recurrence import Recurrence
from ember_code.core.scheduler.runner import SchedulerRunner
from ember_code.core.scheduler.store import TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=tmp_path / "state.db")


# ── SWP-38: missed occurrences collapse ─────────────────────────


class TestCatchUp:
    def test_next_after_without_not_before_is_unchanged(self):
        r = Recurrence.from_canonical("every 1 hours")
        anchor = datetime(2026, 3, 19, 14, 0)
        assert r.next_after(anchor) == datetime(2026, 3, 19, 15, 0)

    def test_missed_occurrences_collapse_to_the_next_future_one(self):
        """48h offline with an hourly task used to yield an anchor that was
        itself in the past — the store handed it straight back as due, once
        per missed hour. Now it lands strictly in the future, on phase."""
        r = Recurrence.from_canonical("every 1 hours")
        anchor = datetime(2026, 3, 19, 14, 0)
        now = anchor + timedelta(hours=48, minutes=10)

        nxt = r.next_after(anchor, not_before=now)

        assert nxt > now
        assert nxt == datetime(2026, 3, 21, 15, 0)  # same :00 phase as the anchor
        assert (nxt - anchor) % r.to_delta() == timedelta(0)

    def test_boundary_occurrence_is_not_reused(self):
        """An occurrence exactly at ``not_before`` is already due — return
        the following one rather than something the store re-fires at once."""
        r = Recurrence.from_canonical("every 30 minutes")
        anchor = datetime(2026, 3, 19, 14, 0)
        nxt = r.next_after(anchor, not_before=datetime(2026, 3, 19, 14, 30))
        assert nxt == datetime(2026, 3, 19, 15, 0)

    @pytest.mark.asyncio
    async def test_runner_reschedules_into_the_future(self, store):
        """End to end: a week-old hourly task reschedules once, ahead of now —
        not 168 immediately-due replays."""
        task = ScheduledTask.new(
            description="hourly chore",
            scheduled_at=datetime.now() - timedelta(days=7),
            recurrence="every 1 hours",
        )
        await store.add(task)
        await store.update_status(task.id, TaskStatus.running)

        runner = SchedulerRunner(store=store, execute_fn=_ok, task_timeout=5)
        await runner._execute_task(task.id, task.description)

        pending = [t for t in await store.get_all() if t.id != task.id]
        assert len(pending) == 1
        assert pending[0].scheduled_at > datetime.now()
        assert not await store.get_due_tasks()


# ── SWP-39: cancel during execution ─────────────────────────────


class TestCancelDuringExecution:
    @pytest.mark.asyncio
    async def test_store_write_loses_to_a_cancel(self, store):
        task = ScheduledTask.new(description="x", scheduled_at=datetime.now())
        await store.add(task)
        await store.update_status(task.id, TaskStatus.cancelled)

        wrote = await store.update_status(
            task.id, TaskStatus.completed, result="done", unless_cancelled=True
        )

        assert wrote is False
        assert (await store.get(task.id)).status == TaskStatus.cancelled

    @pytest.mark.asyncio
    async def test_cancel_mid_run_stops_the_recurrence(self, store):
        """The cancel lands while ``execute_fn`` is still running — the window
        the runner keeps open for up to ``task_timeout`` (300s by default)."""
        task = ScheduledTask.new(
            description="misbehaving",
            scheduled_at=datetime.now(),
            recurrence="every 1 hours",
        )
        await store.add(task)
        await store.update_status(task.id, TaskStatus.running)

        async def cancel_midway(_description: str) -> str:
            await store.update_status(task.id, TaskStatus.cancelled)
            return "result the user no longer wants"

        completions: list[tuple] = []
        runner = SchedulerRunner(
            store=store,
            execute_fn=cancel_midway,
            on_task_completed=lambda *a: completions.append(a),
            task_timeout=5,
        )
        await runner._execute_task(task.id, task.description)

        assert (await store.get(task.id)).status == TaskStatus.cancelled
        assert [t.id for t in await store.get_all(include_done=True)] == [task.id]
        # No "completed" push for a run the user cancelled.
        assert completions == []

    @pytest.mark.asyncio
    async def test_uncancelled_recurring_task_still_reschedules(self, store):
        task = ScheduledTask.new(
            description="fine",
            scheduled_at=datetime.now(),
            recurrence="every 1 hours",
        )
        await store.add(task)
        await store.update_status(task.id, TaskStatus.running)

        runner = SchedulerRunner(store=store, execute_fn=_ok, task_timeout=5)
        await runner._execute_task(task.id, task.description)

        assert (await store.get(task.id)).status == TaskStatus.completed
        assert len(await store.get_all(include_done=True)) == 2


# ── SWP-55: legacy recurrence rows ──────────────────────────────


class TestLegacyRecurrenceRows:
    @pytest.mark.asyncio
    async def test_daily_row_does_not_break_polling(self, store):
        """``daily`` was the model docstring's own example and predates the
        canonical-form validator. Reading such a row used to raise
        ``ValidationError`` out of ``get_due_tasks``, so every task in the
        project stopped running."""
        await _insert_raw(store, id_="legacy", recurrence="daily")

        due = await store.get_due_tasks()

        assert [t.id for t in due] == ["legacy"]
        assert due[0].recurrence == "every 1 days"
        assert due[0].recurrence_obj is not None

    @pytest.mark.asyncio
    async def test_weekly_row_normalizes(self, store):
        await _insert_raw(store, id_="legacy2", recurrence="weekly")
        assert (await store.get("legacy2")).recurrence == "every 7 days"

    @pytest.mark.asyncio
    async def test_unparseable_row_degrades_to_one_shot(self, store):
        await _insert_raw(store, id_="legacy3", recurrence="on a blue moon")
        task = await store.get("legacy3")
        assert task is not None
        assert task.recurrence == ""

    def test_docstring_examples_construct(self):
        """The model docstring names the canonical form; every example in it
        must survive its own validator."""
        for pattern in ("every 1 hours", "every 1 days", "every 7 days"):
            assert ScheduledTask.new("x", datetime.now(), recurrence=pattern)


async def _ok(_description: str) -> str:
    return "ok"


async def _insert_raw(store: TaskStore, *, id_: str, recurrence: str) -> None:
    """Write a row straight through SQLAlchemy, bypassing the pydantic
    validator — that is exactly how the rows in question got there."""
    async with store._db.session() as session, session.begin():
        session.add(
            ScheduledTaskModel(
                id=id_,
                description="legacy task",
                scheduled_at=datetime.now() - timedelta(minutes=1),
                created_at=datetime.now(),
                status=TaskStatus.pending.value,
                result="",
                error="",
                recurrence=recurrence,
            )
        )
