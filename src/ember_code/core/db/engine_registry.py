"""Engine + sessionmaker cache, owned by an :class:`EngineRegistry` instance.

The registry replaces the previous module-level ``_lock`` +  four cache
dicts in ``core/db/engine.py``. State + behaviour live together on the
class so tests can construct their own registry for isolation without
touching a process-global.

Threading + caching invariants:

* Engines are cached by NORMALISED path — two callers passing
  ``~/.ember/state.db`` and the equivalent absolute path get the
  SAME engine. Two engines on the same file would create two
  SQLAlchemy pools on a single SQLite file → lock contention.
* All four cache dicts are guarded by a single ``threading.Lock``.
  The lock protects insertion; engine + sessionmaker construction
  runs inside the critical section (cheap) so we never race two
  ``create_engine`` calls on the same key.

Production callers should use the shared :class:`EngineCache`
singleton (``core/db/engine.py`` exposes ``cache`` and re-binds the
historical module-level call surface to bound methods on it). The
underlying registry is reachable as ``cache.registry``. Per-instance
``EngineRegistry()`` construction is for TEST ISOLATION ONLY —
mixing a fresh registry with the shared ``cache`` on the SAME path
would create two pools on one file (the exact bug the cache exists
to prevent).
"""

from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from ember_code.core.db.urls import _ensure_parent, _normalize_path, async_url, sync_url


class EngineRegistry:
    """Cache of SQLAlchemy engines + sessionmakers keyed by normalised path.

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

    def get_engine(self, path: str | Path) -> Engine:
        key = _normalize_path(path)
        with self._lock:
            engine = self._sync_engines.get(key)
            if engine is None:
                _ensure_parent(path)
                engine = create_engine(
                    sync_url(path),
                    pool_pre_ping=True,
                    future=True,
                )
                self._sync_engines[key] = engine
            return engine

    def get_sessionmaker(self, path: str | Path) -> sessionmaker[Session]:
        key = _normalize_path(path)
        engine = self.get_engine(path)
        with self._lock:
            sm = self._sync_sessionmakers.get(key)
            if sm is None:
                sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)
                self._sync_sessionmakers[key] = sm
            return sm

    def get_async_engine(self, path: str | Path) -> AsyncEngine:
        key = _normalize_path(path)
        with self._lock:
            engine = self._async_engines.get(key)
            if engine is None:
                _ensure_parent(path)
                engine = create_async_engine(
                    async_url(path),
                    pool_pre_ping=True,
                    future=True,
                )
                self._async_engines[key] = engine
            return engine

    def get_async_sessionmaker(self, path: str | Path) -> async_sessionmaker[AsyncSession]:
        key = _normalize_path(path)
        engine = self.get_async_engine(path)
        with self._lock:
            sm = self._async_sessionmakers.get(key)
            if sm is None:
                sm = async_sessionmaker(bind=engine, expire_on_commit=False)
                self._async_sessionmakers[key] = sm
            return sm

    def dispose_sync(self) -> None:
        """Close every cached SYNC engine and clear ALL four caches.

        Async engines are dropped without ``await engine.dispose()`` —
        this is the historical behaviour pinned by
        ``test_clears_async_caches``. Callers that need proper async
        cleanup should ``await`` :meth:`dispose` instead.
        """
        with self._lock:
            for engine in self._sync_engines.values():
                engine.dispose()
            self._sync_engines.clear()
            self._sync_sessionmakers.clear()
            # Async engines must be disposed via their sync helper or in
            # an event loop; the sync-only path drops them without
            # awaiting. See :meth:`dispose` for the async-safe variant.
            self._async_engines.clear()
            self._async_sessionmakers.clear()

    async def dispose(self) -> None:
        """Async-aware disposal: awaits every cached async engine.

        Iterates ``self._async_engines`` and calls ``await
        engine.dispose()`` on each BEFORE clearing the caches, then
        disposes the sync engines too. This closes the gap where the
        sync ``dispose_sync()`` path would drop async engines without
        awaiting their cleanup.
        """
        # Snapshot the async engines under the lock so we don't hold
        # it while awaiting (dispose() is I/O — network flushes on
        # non-SQLite backends, and holding the lock across an await
        # would serialise every other cache access).
        with self._lock:
            async_engines = list(self._async_engines.values())
        for engine in async_engines:
            await engine.dispose()
        with self._lock:
            for engine in self._sync_engines.values():
                engine.dispose()
            self._sync_engines.clear()
            self._sync_sessionmakers.clear()
            self._async_engines.clear()
            self._async_sessionmakers.clear()
