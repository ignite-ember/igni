"""After a restore the pools are swapped at once and the old ones closed later.

``docs/CLOUD_RESUME_TODO.md`` item 1, ``EngineRegistry.reset_pools``. On a
restored VM every page touched before the first answer is one that answer
waits for; closing a pool's connections walks each one's state. So the
reset swaps empty pools in — nothing more — closes the old connections
:attr:`EngineRegistry.RETIRE_AFTER_SECONDS` later, and opens a connection
ahead for an engine that had one, in the moment before a client connects.
Run on SQLite and, with ``IGNI_TEST_DATABASE_URL``, PostgreSQL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import event

from ember_code.core.db.engine_registry import EngineRegistry
from ember_code.core.db.urls import DatabaseUrl


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> EngineRegistry:
    monkeypatch.setattr(EngineRegistry, "RETIRE_AFTER_SECONDS", 0.05)
    return EngineRegistry()


async def test_the_old_connections_are_closed_later_not_at_once(
    registry: EngineRegistry, database_url: DatabaseUrl
) -> None:
    engine = registry.get_async_engine(database_url)
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    old = engine.sync_engine.pool
    closed: list[object] = []
    event.listen(old, "close", lambda dbapi, record: closed.append(dbapi))

    await registry.reset_pools()

    assert engine.sync_engine.pool is not old, "an empty pool, at once"
    assert closed == [], "the old connection is not closed on the way to the first request"
    await asyncio.sleep(EngineRegistry.RETIRE_AFTER_SECONDS + 0.2)
    assert len(closed) == 1, "closed a moment later"
    async with engine.connect() as connection:
        assert (await connection.exec_driver_sql("SELECT 1")).scalar() == 1
    await registry.dispose()


async def test_an_engine_that_had_a_connection_gets_one_ahead(
    registry: EngineRegistry, database_url: DatabaseUrl, tmp_path: Path
) -> None:
    engine = registry.get_async_engine(database_url)
    idle = registry.get_async_engine(DatabaseUrl.for_file(tmp_path / "idle.db"))
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")

    await registry.reset_pools()
    for _ in range(100):
        if engine.sync_engine.pool.checkedin():
            break
        await asyncio.sleep(0.01)

    assert engine.sync_engine.pool.checkedin() == 1, "waiting for the first query"
    assert idle.sync_engine.pool.checkedin() == 0, "nothing opened for an engine never used"
    await registry.dispose()


async def test_disposing_closes_what_waits_to_be_closed(
    registry: EngineRegistry, database_url: DatabaseUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(EngineRegistry, "RETIRE_AFTER_SECONDS", 3600.0)
    engine = registry.get_engine(database_url)
    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")
    old = engine.pool
    closed: list[object] = []
    event.listen(old, "close", lambda dbapi, record: closed.append(dbapi))

    await registry.reset_pools()
    await registry.dispose()

    assert len(closed) == 1
