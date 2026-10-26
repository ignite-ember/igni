"""SQLAlchemy engine + sessionmaker access — owned by an :class:`EngineCache` instance.

The canonical entry point is :class:`EngineCache`. It composes an
:class:`ember_code.core.db.engine_registry.EngineRegistry` (which owns the
real engine + sessionmaker caches + lock) and exposes them as instance
methods so the historical module-level call surface (``get_engine``,
``get_sessionmaker``, ``get_async_engine``, ``get_async_sessionmaker``,
``dispose_all``) becomes a single shared object — not five free functions
reaching into module globals.

Keyed by :class:`~ember_code.core.db.urls.DatabaseUrl`: a SQLite file
on the desktop (per-project ``state.db`` plus the global ``ember.db``),
the configured PostgreSQL database in the cloud. Both sync and async
flavours are exported. Engines and sessionmakers are cached per
database so callers can request them freely without pool churn —
alembic and the small key/value stores use the sync flavour; the
async stores use the async one (``aiosqlite`` / psycopg).

Back-compat: ``database.py``, ``migrations.py``, and existing tests
import the five names from this module. We keep them working by
attaching bound-method aliases at import time so legacy
``from ember_code.core.db.engine import get_engine`` calls still
resolve. New callers should ``from ember_code.core.db.engine import
cache`` and call ``cache.get_engine(target)`` directly.

The module-level :data:`cache` is a single shared :class:`EngineCache`
instance — same singleton convention as :data:`migrator` in
``migrations.py``. Tests that need isolation should construct their
own ``EngineCache(EngineRegistry())`` instead of touching the shared
one (mixing a fresh registry with the shared cache on the SAME path
would create two pools on one file — the exact bug the cache exists
to prevent).
"""

from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from ember_code.core.db.engine_registry import DatabaseTarget, EngineRegistry

__all__ = [
    "EngineCache",
    "EngineRegistry",
    "cache",
    "dispose_all",
    "get_async_engine",
    "get_async_sessionmaker",
    "get_engine",
    "get_sessionmaker",
]


class EngineCache:
    """Coordinator that exposes an :class:`EngineRegistry` as instance methods.

    Owns the registry so the historical module-level call surface
    (``get_engine`` / ``get_sessionmaker`` / etc.) is real OOP — each
    name is a bound method on a class instance, not a free function
    reaching into a module global. The underlying caches + lock still
    live on the injected :class:`EngineRegistry`.
    """

    def __init__(self, registry: EngineRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> EngineRegistry:
        """The underlying :class:`EngineRegistry` — exposed for callers
        that already speak the registry API directly (e.g. ``cache.registry``
        replaces the historical ``engine.registry`` module global)."""
        return self._registry

    def get_engine(self, target: DatabaseTarget) -> Engine:
        return self._registry.get_engine(target)

    def get_sessionmaker(self, target: DatabaseTarget) -> sessionmaker[Session]:
        return self._registry.get_sessionmaker(target)

    def get_async_engine(self, target: DatabaseTarget) -> AsyncEngine:
        return self._registry.get_async_engine(target)

    def get_async_sessionmaker(self, target: DatabaseTarget) -> async_sessionmaker[AsyncSession]:
        return self._registry.get_async_sessionmaker(target)

    def dispose_all(self) -> None:
        """Close every cached engine — used in tests and on shutdown.

        Delegates to :meth:`EngineRegistry.dispose_sync` (NOT the async
        :meth:`EngineRegistry.dispose`) to preserve the historical
        sync-drop-without-await behaviour for async engines.
        """
        self._registry.dispose_sync()


# Module-level singleton — production-wide shared cache. Tests and
# callers that need isolation should construct their own
# ``EngineCache(EngineRegistry())`` instead of touching this one.
cache = EngineCache(EngineRegistry())


# Back-compat: legacy callers (database.py, migrations.py, tests) still
# ``from ember_code.core.db.engine import get_engine``. Re-bind the
# five methods as module-level names so those imports keep working
# without forcing every caller to switch to ``cache.get_engine``.
# These are BOUND METHODS on ``cache`` (an instance attribute), not
# free functions — the OOP shape is preserved even at the import
# surface.
get_engine = cache.get_engine
get_sessionmaker = cache.get_sessionmaker
get_async_engine = cache.get_async_engine
get_async_sessionmaker = cache.get_async_sessionmaker
dispose_all = cache.dispose_all
