"""A database handle: which database, and whose rows in it.

Each instance names one database — a project's ``state.db`` or the
global ``ember.db`` on the desktop, the configured PostgreSQL database
in the cloud (:class:`~ember_code.core.db.urls.DatabaseUrl`) — and the
:class:`~ember_code.core.db.scope.StorageScope` its stores read and
write. Construction makes sure the schema matches the ORM models: it
reads the database's revision, and only one that is new or behind gets
``alembic upgrade head`` (see :mod:`ember_code.core.db.schema`).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from ember_code.core.db.engine import get_async_sessionmaker, get_sessionmaker
from ember_code.core.db.engine_registry import DatabaseTarget
from ember_code.core.db.schema import keeper
from ember_code.core.db.scope import StorageScope
from ember_code.core.db.urls import DatabaseUrl


class Database:
    def __init__(
        self,
        target: DatabaseTarget,
        *,
        scope: StorageScope | None = None,
        run_migrations: bool = True,
    ) -> None:
        self.url = DatabaseUrl.of(target)
        self.scope = scope if scope is not None else StorageScope.local()
        if run_migrations:
            keeper.ensure_current(self.url)
        self._sessionmaker: async_sessionmaker[AsyncSession] = get_async_sessionmaker(self.url)
        self._sync_sessionmaker: sessionmaker[Session] = get_sessionmaker(self.url)

    @classmethod
    def of(cls, target: Database | DatabaseTarget) -> Database:
        """``target`` if it is a handle already, else a desktop handle on it."""
        return target if isinstance(target, Database) else cls(target)

    @property
    def path(self) -> Path | None:
        """The SQLite file, or ``None`` for a server."""
        return self.url.file

    def in_scope(self, scope: StorageScope) -> Database:
        """The same database, for another scope's rows."""
        return Database(self.url, scope=scope, run_migrations=False)

    def session(self) -> AsyncSession:
        """Open a new async session.

        Use as ``async with db.session() as s: ...`` — SA closes the
        session on exit and rolls back any uncommitted state.
        """
        return self._sessionmaker()

    def sync_session(self) -> Session:
        """Open a new sync session — for the small stores whose callers
        are synchronous (they reach it through ``asyncio.to_thread``)."""
        return self._sync_sessionmaker()
