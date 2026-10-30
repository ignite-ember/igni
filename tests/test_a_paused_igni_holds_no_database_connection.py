"""A paused igni holds no database connection; the first query after opens one.

``docs/CLOUD.md`` §9. A node pauses about a thousand VMs, and each
producer connects with its own login, which no pooler can share: igni
closes every pooled connection just before a pause
(:meth:`EngineRegistry.close_connections`, run by the before-suspend
hook), and nothing reconnects until a query wants a connection. The
server keepalives too, so a VM killed holding one does not hold the
server's for long. PostgreSQL only for the server side; skipped without
``IGNI_TEST_DATABASE_URL``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, text

from ember_code.core.db.engine_registry import EngineRegistry, PostgresConnectionSetup
from ember_code.core.db.urls import DatabaseUrl, Dialect


async def test_before_a_pause_every_connection_closes_and_the_next_query_opens_one(
    database_url: DatabaseUrl,
) -> None:
    registry = EngineRegistry()
    engine = registry.get_async_engine(database_url)
    sync_engine = registry.get_engine(database_url)
    closed: list[Any] = []
    opened: list[Any] = []
    for target in (engine.sync_engine, sync_engine):
        event.listen(target, "close", lambda dbapi, record: closed.append(dbapi))
        event.listen(target, "connect", lambda dbapi, record: opened.append(dbapi))
    async with engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")
    assert len(opened) == 2

    await registry.close_connections()

    assert len(closed) == 2, "nothing held while paused"
    assert engine.sync_engine.pool.checkedin() == 0
    async with engine.connect() as connection:
        assert (await connection.exec_driver_sql("SELECT 1")).scalar() == 1
    assert len(opened) == 3, "the first query after opened one"
    await registry.dispose()


async def test_the_server_keepalives_igni_s_connections(database_url: DatabaseUrl) -> None:
    if database_url.dialect is not Dialect.POSTGRESQL:
        return
    registry = EngineRegistry()
    engine = registry.get_async_engine(database_url)
    async with engine.connect() as connection:
        idle = (await connection.execute(text("SHOW tcp_keepalives_idle"))).scalar()
    assert str(idle).rstrip("s") == str(PostgresConnectionSetup.KEEPALIVE_IDLE_SECONDS)
    await registry.dispose()
