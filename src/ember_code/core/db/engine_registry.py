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

import asyncio
import logging
import threading
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
from sqlalchemy.pool import Pool
from sqlalchemy.util import greenlet_spawn

from ember_code.core.activity import background
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
    the pool opens a fresh one (``docs/CLOUD.md`` §9). libpq parameters,
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
        #: Old pools waiting to be closed (:meth:`reset_pools`): sync, async.
        self._retired: tuple[list[Pool], list[Pool]] = ([], [])
        self._retiring: asyncio.Task[None] | None = None
        #: Connections being opened ahead after a reset.
        self._warming: set[asyncio.Task[None]] = set()

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

    #: How long closing one engine's pooled connections may take before
    #: they are dropped unclosed.
    RESET_TIMEOUT_SECONDS = 5.0
    #: How long after a reset the old pools' connections are closed.
    RETIRE_AFTER_SECONDS = 2.0

    async def reset_pools(self) -> None:
        """Give every engine an empty pool; close the old pools' connections later.

        After a snapshot restore (``docs/CLOUD.md`` §9) a pooled
        connection may be to a server that dropped it while the VM was
        away. Stores keep their engines, so the engines stay and only
        their pools are replaced: the next query opens a fresh
        connection. The swap is all that happens now — it touches
        nothing but the engines. Closing the old connections walks
        each one's state (and, on a restored VM, faults it back in),
        so it waits :attr:`RETIRE_AFTER_SECONDS`, off the path of the
        first requests; a close that does not finish in
        :attr:`RESET_TIMEOUT_SECONDS` leaves the connections to the
        garbage collector rather than wait on a dead socket.

        An async engine that had connections gets one again at once, in
        the background: a restored VM is idle for the moment between
        its restore and its first client connecting, and the first
        query then finds the connection open.
        """
        with self._lock:
            sync_engines: list[Engine] = list(self._sync_engines.values())
            async_engines: list[AsyncEngine] = list(self._async_engines.values())
        self._retired[0].extend(self._swap_pool(engine) for engine in sync_engines)
        for engine in async_engines:
            old = self._swap_pool(engine.sync_engine)
            self._retired[1].append(old)
            if old.checkedin():
                warming = background.daemon(self._warm(engine), name="warm-pool")
                self._warming.add(warming)
                warming.add_done_callback(self._warming.discard)
        if self._retiring is None or self._retiring.done():
            self._retiring = background.daemon(self._close_retired_later(), name="retired-pools")

    async def _warm(self, engine: AsyncEngine) -> None:
        """One connection opened and put back in ``engine``'s new pool."""
        try:
            async with asyncio.timeout(self.RESET_TIMEOUT_SECONDS):
                async with engine.connect():
                    pass
        except Exception as exc:
            logger.info("could not open a connection ahead for %s: %s", engine.url, exc)

    @staticmethod
    def _swap_pool(engine: Engine) -> Pool:
        """What :meth:`Engine.dispose` does, minus closing: the old pool is returned."""
        old = engine.pool
        engine.pool = old.recreate()
        return old

    async def _close_retired_later(self) -> None:
        await asyncio.sleep(self.RETIRE_AFTER_SECONDS)
        await self._close_retired()

    async def _close_retired(self) -> None:
        retired, retired_async = self._retired
        self._retired = ([], [])
        for pool in retired:
            pool.dispose()
        for pool in retired_async:
            try:
                await asyncio.wait_for(
                    greenlet_spawn(pool.dispose), timeout=self.RESET_TIMEOUT_SECONDS
                )
            except Exception as exc:
                logger.info("dropping a retired pool's connections unclosed: %s", exc)

    async def dispose(self) -> None:
        """Async-aware disposal: awaits every cached async engine, then
        disposes the sync engines and clears every cache."""
        # Snapshot under the lock, await outside it: dispose() is I/O
        # (network flushes on PostgreSQL), and holding the lock across
        # an await would serialise every other cache access.
        with self._lock:
            async_engines: list[AsyncEngine] = list(self._async_engines.values())
        if self._retiring is not None:
            self._retiring.cancel()
            self._retiring = None
        for warming in list(self._warming):
            warming.cancel()
        await self._close_retired()
        for engine in async_engines:
            await engine.dispose()
        self.dispose_sync()


__all__ = [
    "DatabaseTarget",
    "EngineRegistry",
    "PostgresConnectionSetup",
    "SqliteConnectionSetup",
]
