"""After a restore the pools are marked stale, and nothing more happens until a query.

``docs/CLOUD_RESUME_TODO.md`` item 1, ``EngineRegistry.reset_pools``. On a
restored VM every page touched before the first answer is one that
answer waits for, so the reset closes and opens nothing: it marks every
pooled connection as older than the restore, and SQLAlchemy recycles
one — closes it, opens a fresh one — when a query next checks it out,
before any pre-ping could wait on a connection whose packets go nowhere.
Run on SQLite and, with ``IGNI_TEST_DATABASE_URL``, PostgreSQL.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event

from ember_code.core.db.engine_registry import EngineRegistry
from ember_code.core.db.urls import DatabaseUrl


class Seen:
    """Every connection a pool closes and opens."""

    def __init__(self, engine: Any) -> None:
        self.closed: list[object] = []
        self.opened: list[object] = []
        event.listen(engine, "close", lambda dbapi, record: self.closed.append(dbapi))
        event.listen(engine, "connect", lambda dbapi, record: self.opened.append(dbapi))


async def test_a_reset_closes_and_opens_nothing_until_a_query(database_url: DatabaseUrl) -> None:
    registry = EngineRegistry()
    engine = registry.get_async_engine(database_url)
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    pool = engine.sync_engine.pool
    seen = Seen(engine.sync_engine)

    await registry.reset_pools()

    assert engine.sync_engine.pool is pool, "the same pool"
    assert (seen.closed, seen.opened) == ([], []), "nothing on the way to the first answer"
    async with engine.connect() as connection:
        assert (await connection.exec_driver_sql("SELECT 1")).scalar() == 1
    assert len(seen.closed) == 1 and len(seen.opened) == 1, "the old one recycled at checkout"
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    assert len(seen.opened) == 1, "the fresh one is kept"
    await registry.dispose()


async def test_a_sync_engine_too(database_url: DatabaseUrl) -> None:
    registry = EngineRegistry()
    engine = registry.get_engine(database_url)
    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")
    seen = Seen(engine)
    await registry.reset_pools()
    assert seen.closed == []
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1
    assert len(seen.closed) == 1 and len(seen.opened) == 1
    await registry.dispose()
