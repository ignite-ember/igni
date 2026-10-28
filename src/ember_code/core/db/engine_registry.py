"""Engine + sessionmaker cache, owned by an :class:`EngineRegistry` instance.

State + behaviour live together on the class so tests can construct
their own registry for isolation without touching a process-global.

Threading + caching invariants:

* Engines are cached by :class:`~ember_code.core.db.urls.DatabaseUrl` —
  for a SQLite file its NORMALISED path, so two callers passing
  ``~/.igni/state.db`` and the equivalent absolute path get the SAME
  engine. Two engines on the same file would create two SQLAlchemy
  pools on a single SQLite file → lock contention.
* All four cache dicts are guarded by a single ``threading.Lock``.
  The lock protects insertion; engine + sessionmaker construction
  runs inside the critical section (cheap) so we never race two
  ``create_engine`` calls on the same key.

The registry is the one place that knows dialects apart beyond the URL.
A SQLite connection is opened with a five-second busy timeout and put
in WAL mode, so readers do not block the writer and a contended write
waits instead of failing with ``database is locked`` — what the raw
``sqlite3`` stores used to set on every connection of their own.
PostgreSQL needs neither; its connections get TCP keepalives and a
timeout instead (:class:`PostgresConnectionSetup`), so a connection a
snapshot restore left dead fails fast and the pool replaces it.

Production callers should use the shared :class:`EngineCache`
singleton (``core/db/engine.py`` exposes ``cache``). Per-instance
``EngineRegistry()`` construction is for TEST ISOLATION ONLY — mixing
a fresh registry with the shared ``cache`` on the SAME database would
create two pools on it (the exact bug the cache exists to prevent).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from ember_code.core.db.urls import DatabaseUrl

logger = logging.getLogger(__name__)

#: Target for :meth:`EngineRegistry.get_engine` and friends.
DatabaseTarget = str | Path | DatabaseUrl


class SqliteConnectionSetup:
    """What every SQLite connection igni opens is set up with."""

    #: Seconds a write waits on another connection's lock before failing.
    #: Matches the backend's per-RPC budget: past this the file really is
    #: wedged and failing the call is the right outcome.
    BUSY_TIMEOUT_SECONDS = 5.0

    @classmethod
    def connect_args(cls) -> dict[str, Any]:
        return {"timeout": cls.BUSY_TIMEOUT_SECONDS}

    @staticmethod
    def on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    @classmethod
    def attach(cls, engine: Engine) -> None:
        event.listen(engine, "connect", cls.on_connect)


class PostgresConnectionSetup:
    """What every PostgreSQL connection igni opens is set up with.

    A connection whose server or network went away without a word — a
    VM restored from a snapshot, a NAT entry that expired while it
    slept — fails within seconds instead of the kernel's quarter of an
    hour of retransmissions: the pool's pre-ping on it errors out and
    the pool opens a fresh one (``docs/CLOUD.md`` §9). The server
    keepalives too (``options``), so a VM that died holding a connection
    does not hold the server's for long. libpq parameters,
    which psycopg takes for its sync and async connections alike; a
    platform without ``TCP_USER_TIMEOUT`` ignores that one.
    """

    CONNECT_TIMEOUT_SECONDS = 10
    KEEPALIVE_IDLE_SECONDS = 30
    KEEPALIVE_INTERVAL_SECONDS = 10
    KEEPALIVE_COUNT = 3
    USER_TIMEOUT_MS = 30_000

    @classmethod
    def connect_args(cls) -> dict[str, Any]:
        return {
            "connect_timeout": cls.CONNECT_TIMEOUT_SECONDS,
            "keepalives": 1,
            "keepalives_idle": cls.KEEPALIVE_IDLE_SECONDS,
            "keepalives_interval": cls.KEEPALIVE_INTERVAL_SECONDS,
            "keepalives_count": cls.KEEPALIVE_COUNT,
            "tcp_user_timeout": cls.USER_TIMEOUT_MS,
            # The server probes too: a VM killed without a word frees its
            # connection on the server within about a minute.
            "options": (
                f"-c tcp_keepalives_idle={cls.KEEPALIVE_IDLE_SECONDS} "
                f"-c tcp_keepalives_interval={cls.KEEPALIVE_INTERVAL_SECONDS} "
                f"-c tcp_keepalives_count={cls.KEEPALIVE_COUNT}"
            ),
        }


class EngineRegistry:
    """Cache of SQLAlchemy engines + sessionmakers keyed by database.

    One instance owns the state for both sync and async flavours. The
    caches are independent across instances — see the module docstring
    for the "use the singleton in production" caveat.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sync_engines: dict[str, Engine] = {}
        self._sync_sessionmakers: dict[str, sessionmaker[Session]] = {}
        self._async_engines: dict[str, AsyncEngine] = {}
        self._async_sessionmakers: dict[str, async_sessionmaker[AsyncSession]] = {}

    def get_engine(self, target: DatabaseTarget) -> Engine:
        url = DatabaseUrl.of(target)
        with self._lock:
            engine = self._sync_engines.get(url.url)
            if engine is None:
                url.prepare()
                if url.is_sqlite:
                    engine = create_engine(
                        url.sync,
                        pool_pre_ping=True,
                        connect_args=SqliteConnectionSetup.connect_args(),
                    )
                    SqliteConnectionSetup.attach(engine)
                else:
                    engine = create_engine(
                        url.sync,
                        pool_pre_ping=True,
                        connect_args=PostgresConnectionSetup.connect_args(),
                    )
                self._sync_engines[url.url] = engine
            return engine

    def get_sessionmaker(self, target: DatabaseTarget) -> sessionmaker[Session]:
        url = DatabaseUrl.of(target)
        engine = self.get_engine(url)
        with self._lock:
            sm = self._sync_sessionmakers.get(url.url)
            if sm is None:
                sm = sessionmaker(bind=engine, expire_on_commit=False)
                self._sync_sessionmakers[url.url] = sm
            return sm

    def get_async_engine(self, target: DatabaseTarget) -> AsyncEngine:
        url = DatabaseUrl.of(target)
        with self._lock:
            engine = self._async_engines.get(url.url)
            if engine is None:
                url.prepare()
                if url.is_sqlite:
                    engine = create_async_engine(
                        url.async_,
                        pool_pre_ping=True,
                        connect_args=SqliteConnectionSetup.connect_args(),
                    )
                    SqliteConnectionSetup.attach(engine.sync_engine)
                else:
                    engine = create_async_engine(
                        url.async_,
                        pool_pre_ping=True,
                        connect_args=PostgresConnectionSetup.connect_args(),
                    )
                self._async_engines[url.url] = engine
            return engine

    def get_async_sessionmaker(self, target: DatabaseTarget) -> async_sessionmaker[AsyncSession]:
        url = DatabaseUrl.of(target)
        engine = self.get_async_engine(url)
        with self._lock:
            sm = self._async_sessionmakers.get(url.url)
            if sm is None:
                sm = async_sessionmaker(bind=engine, expire_on_commit=False)
                self._async_sessionmakers[url.url] = sm
            return sm

    def dispose_sync(self) -> None:
        """Close every cached SYNC engine and clear ALL four caches.

        Async engines are dropped without ``await engine.dispose()`` —
        this is the historical behaviour pinned by
        ``test_clears_async_caches``. Callers that need proper async
        cleanup should ``await`` :meth:`dispose` instead.
        """
        with self._lock:
            for sync_engine in self._sync_engines.values():
                sync_engine.dispose()
            self._sync_engines.clear()
            self._sync_sessionmakers.clear()
            self._async_engines.clear()
            self._async_sessionmakers.clear()

    async def close_connections(self) -> None:
        """Close every connection the pools hold now; keep the engines.

        Before a VM is paused (``docs/CLOUD.md`` §9): a paused VM holds no
        connection to its database — a node pauses about a thousand, and
        each producer's own login cannot share a pooler. The pools stay,
        empty, and the first query afterwards opens a fresh connection.
        """
        with self._lock:
            sync_engines: list[Engine] = list(self._sync_engines.values())
            async_engines: list[AsyncEngine] = list(self._async_engines.values())
        for engine in sync_engines:
            engine.dispose()
        for async_engine in async_engines:
            await async_engine.dispose()

    async def reset_pools(self) -> None:
        """Mark every pooled connection as opened before now; nothing more.

        After a snapshot restore (``docs/CLOUD.md`` §9) a pooled
        connection may be to a server that dropped it while the VM was
        away — or to one whose packets now go nowhere, which a ping would
        wait out. So each pool is told that everything it holds predates
        this moment: SQLAlchemy's own pool-wide invalidation, which
        recycles a connection — closes it and opens a fresh one — when it
        is next checked out, before the pre-ping. It costs nothing now: on
        a restored VM every page touched before the first answer is one
        that answer waits for, and no connection is closed or opened until
        a query wants one. Engines and pools stay.
        """
        with self._lock:
            engines: list[Engine] = list(self._sync_engines.values())
            engines += [engine.sync_engine for engine in self._async_engines.values()]
        now = time.time()
        for engine in engines:
            # ``Pool._invalidate_time``: what ``Pool._invalidate`` sets for
            # every connection at once; a record started before it is
            # recycled at its next checkout.
            engine.pool._invalidate_time = now  # type: ignore[attr-defined]

    async def dispose(self) -> None:
        """Async-aware disposal: awaits every cached async engine, then
        disposes the sync engines and clears every cache."""
        # Snapshot under the lock, await outside it: dispose() is I/O
        # (network flushes on PostgreSQL), and holding the lock across
        # an await would serialise every other cache access.
        with self._lock:
            async_engines: list[AsyncEngine] = list(self._async_engines.values())
        for engine in async_engines:
            await engine.dispose()
        self.dispose_sync()


__all__ = [
    "DatabaseTarget",
    "EngineRegistry",
    "PostgresConnectionSetup",
    "SqliteConnectionSetup",
]
