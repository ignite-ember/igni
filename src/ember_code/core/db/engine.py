"""SQLAlchemy engine + sessionmaker access — owned by an :class:`EngineCache` instance.

The canonical entry point is :class:`EngineCache`. It composes an
:class:`ember_code.core.db.engine_registry.EngineRegistry` (which owns the
real engine + sessionmaker caches + lock) and exposes them as instance
methods so the historical module-level call surface (``get_engine``,
``get_sessionmaker``, ``get_async_engine``, ``get_async_sessionmaker``,
``dispose_all``) becomes a single shared object — not five free functions
reaching into module globals.

SQLite-only (per-project ``state.db`` files plus a global ``ember.db``).
Both sync and async flavours are exported. Engines and sessionmakers
are cached by normalised path so callers can request them freely
without pool churn — alembic uses the sync flavour under the hood;
everything in code_index uses the async flavour via ``aiosqlite``.

Back-compat: ``database.py``, ``migrations.py``, and existing tests
import the five names from this module. We keep them working by
attaching bound-method aliases at import time so legacy
``from ember_code.core.db.engine import get_engine`` calls still
resolve. New callers should ``from ember_code.core.db.engine import
cache`` and call ``cache.get_engine(path)`` directly.

The module-level :data:`cache` is a single shared :class:`EngineCache`
instance — same singleton convention as :data:`migrator` in
``migrations.py``. Tests that need isolation should construct their
own ``EngineCache(EngineRegistry())`` instead of touching the shared
one (mixing a fresh registry with the shared cache on the SAME path
would create two pools on one file — the exact bug the cache exists
to prevent).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from ember_code.core.db.engine_registry import EngineRegistry
from ember_code.core.db.urls import _ensure_parent, _normalize_path, async_url, sync_url

__all__ = [
    "EngineCache",
    "EngineRegistry",
    "_ensure_parent",
    "_normalize_path",
    "async_url",
    "cache",
    "dispose_all",
    "get_async_engine",
    "get_async_sessionmaker",
    "get_engine",
    "get_sessionmaker",
    "sync_url",
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

    def get_engine(self, path: str | Path) -> Engine:
        return self._registry.get_engine(path)

    def get_sessionmaker(self, path: str | Path) -> sessionmaker[Session]:
        return self._registry.get_sessionmaker(path)

    def get_async_engine(self, path: str | Path) -> AsyncEngine:
        return self._registry.get_async_engine(path)

    def get_async_sessionmaker(self, path: str | Path) -> async_sessionmaker[AsyncSession]:
        return self._registry.get_async_sessionmaker(path)

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
