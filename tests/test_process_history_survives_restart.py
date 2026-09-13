"""What ran here, and how did it go — across BE restarts.

``background_processes`` was a liveness table. A row existed while a
process ran, completion deleted it, and startup pruned any row whose
pid was gone. So the watcher could answer "what is running" and nothing
else: a build that failed two minutes ago had already been erased, and
restarting the BE erased the rest.

The table now records endings (``exit_code``, ``finished_at``) and keeps
them until a retention window expires. These tests cover the round trip
that matters — write an ending, restart, read it back — plus the two
edges: a process that ended while the BE was not running, and history
old enough to forget.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ember_code.core.tools.orphan_rehydrator import (
    HISTORY_RETENTION_SECONDS,
    OrphanRehydrator,
)
from ember_code.core.tools.process_store import (
    BackgroundProcessRow,
    BackgroundProcessStore,
)
from ember_code.core.tools.process_supervisor import ProcessSupervisor

DEAD_PID = 0x7FFFFFFE


@pytest.fixture
def store(tmp_path: Path) -> BackgroundProcessStore:
    return BackgroundProcessStore(db_path=tmp_path / "state.db")


async def test_an_ending_is_written_and_read_back(store):
    """The round trip the whole feature rests on."""
    await store.upsert(
        BackgroundProcessRow(pid=DEAD_PID, cmd="pytest -x", pgid=None, started_at=100)
    )

    await store.finish(DEAD_PID, 1, now=160)

    (row,) = await store.list_all()
    assert row.exit_code == 1
    assert row.finished_at == 160
    assert row.is_finished


async def test_history_survives_a_restart(store, tmp_path):
    """A fresh supervisor — which is what a BE restart is — still sees
    what the previous one ran."""
    await store.upsert(
        BackgroundProcessRow(pid=DEAD_PID, cmd="npm run build", pgid=None, started_at=100)
    )
    await store.finish(DEAD_PID, 0, now=140)

    fresh = ProcessSupervisor()
    result = await OrphanRehydrator(fresh, store).run()

    assert result.ok
    entry = fresh.registry.get(DEAD_PID)
    assert entry is not None, "history did not survive the restart"
    assert not entry.is_running()
    assert entry.returncode() == 0
    assert entry.cmd == "npm run build"
    # Ran for 40 seconds — not "40 seconds ago".
    assert entry.elapsed() == pytest.approx(40.0)


async def test_the_watcher_would_list_it_as_stopped(store):
    """Surviving is not enough; it has to reach the panel's list."""
    from ember_code.backend.server_processes import ProcessesController

    await store.upsert(
        BackgroundProcessRow(pid=DEAD_PID, cmd="make lint", pgid=None, started_at=100)
    )
    await store.finish(DEAD_PID, 2, now=130)

    fresh = ProcessSupervisor()
    await OrphanRehydrator(fresh, store).run()

    rows = ProcessesController(supervisor=fresh).list()
    assert [(r.cmd, r.is_running, r.exit_code) for r in rows] == [("make lint", False, 2)]


async def test_a_process_that_ended_unobserved_says_so(store):
    """The BE exited while it was running, so nobody saw the ending.

    The row is stamped as finished with an unknown code rather than
    deleted (which loses it) or given a fabricated 0 (which lies).
    """
    await store.upsert(
        BackgroundProcessRow(pid=DEAD_PID, cmd="tail -f log", pgid=None, started_at=100)
    )

    fresh = ProcessSupervisor()
    await OrphanRehydrator(fresh, store).run()

    (row,) = await store.list_all()
    assert row.finished_at is not None, "the ending was not recorded"
    assert row.exit_code is None, "an unobserved ending must not claim a code"
    assert fresh.registry.get(DEAD_PID) is not None


async def test_history_past_the_retention_window_is_dropped(store):
    """Bounded, or the table grows for the life of the project."""
    long_ago = int(time.time()) - int(HISTORY_RETENTION_SECONDS) - 60
    await store.upsert(
        BackgroundProcessRow(pid=DEAD_PID, cmd="ancient", pgid=None, started_at=long_ago)
    )
    await store.finish(DEAD_PID, 0, now=long_ago)

    fresh = ProcessSupervisor()
    result = await OrphanRehydrator(fresh, store).run()

    assert result.pruned == 1
    assert await store.list_all() == []


async def test_a_running_process_is_never_too_old_to_keep(store):
    """Retention is about endings. Something started last month and
    still running is not history and must not be pruned."""
    import os

    long_ago = int(time.time()) - int(HISTORY_RETENTION_SECONDS) - 60
    await store.upsert(
        BackgroundProcessRow(
            pid=os.getpid(), cmd="very long server", pgid=None, started_at=long_ago
        )
    )

    fresh = ProcessSupervisor()
    await OrphanRehydrator(fresh, store).run()

    rows = await store.list_all()
    assert len(rows) == 1, "a running process was pruned as if it were history"
