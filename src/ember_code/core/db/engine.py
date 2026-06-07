"""SQLAlchemy engine + sessionmaker factories.

SQLite-only (per-project ``state.db`` files plus a global ``ember.db``).
Both sync and async flavors are exported. Engines and sessionmakers are
cached per (path, mode) so callers can request them freely without pool
churn — alembic uses the sync flavor under the hood; everything in
code_index uses the async flavor via ``aiosqlite``.
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

_lock = threading.Lock()
_sync_engines: dict[str, Engine] = {}
_sync_sessionmakers: dict[str, sessionmaker[Session]] = {}
_async_engines: dict[str, AsyncEngine] = {}
_async_sessionmakers: dict[str, async_sessionmaker[AsyncSession]] = {}


def _normalize_path(path: str | Path) -> str:
    """Resolve ``~`` and relative paths to a canonical absolute path."""
    return str(Path(str(path)).expanduser().resolve())


def sync_url(path: str | Path) -> str:
    """SQLAlchemy URL for sync access (used by alembic)."""
    return f"sqlite:///{_normalize_path(path)}"


def async_url(path: str | Path) -> str:
    """SQLAlchemy URL for async access via aiosqlite."""
    return f"sqlite+aiosqlite:///{_normalize_path(path)}"


def _ensure_parent(path: str | Path) -> None:
    Path(_normalize_path(path)).parent.mkdir(parents=True, exist_ok=True)


def get_engine(path: str | Path) -> Engine:
    key = _normalize_path(path)
    with _lock:
        engine = _sync_engines.get(key)
        if engine is None:
            _ensure_parent(path)
            engine = create_engine(
                sync_url(path),
                pool_pre_ping=True,
                future=True,
            )
            _sync_engines[key] = engine
        return engine


def get_sessionmaker(path: str | Path) -> sessionmaker[Session]:
    key = _normalize_path(path)
    engine = get_engine(path)
    with _lock:
        sm = _sync_sessionmakers.get(key)
        if sm is None:
            sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)
            _sync_sessionmakers[key] = sm
        return sm


def get_async_engine(path: str | Path) -> AsyncEngine:
    key = _normalize_path(path)
    with _lock:
        engine = _async_engines.get(key)
        if engine is None:
            _ensure_parent(path)
            engine = create_async_engine(
                async_url(path),
                pool_pre_ping=True,
                future=True,
            )
            _async_engines[key] = engine
        return engine


def get_async_sessionmaker(path: str | Path) -> async_sessionmaker[AsyncSession]:
    key = _normalize_path(path)
    engine = get_async_engine(path)
    with _lock:
        sm = _async_sessionmakers.get(key)
        if sm is None:
            sm = async_sessionmaker(bind=engine, expire_on_commit=False)
            _async_sessionmakers[key] = sm
        return sm


def dispose_all() -> None:
    """Close every cached engine — used in tests and on shutdown."""
    with _lock:
        for engine in _sync_engines.values():
            engine.dispose()
        _sync_engines.clear()
        _sync_sessionmakers.clear()
        # Async engines must be disposed via their sync helper or in an event
        # loop; callers expecting async cleanup should ``await engine.dispose()``.
        _async_engines.clear()
        _async_sessionmakers.clear()
