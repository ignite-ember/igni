"""Async database handle backed by a single SQLite file.

Each instance owns one SQLite file (per-project ``state.db`` for project
state, or the global ``ember.db`` for memory + learning). Construction
makes sure the schema matches the ORM models: it reads the file's
revision with :mod:`sqlite3`, and only a file that is new or behind gets
``alembic upgrade head`` (see :mod:`ember_code.core.db.schema`).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ember_code.core.db.engine import get_async_sessionmaker
from ember_code.core.db.schema import keeper


class Database:
    def __init__(self, db_path: str | Path, *, run_migrations: bool = True):
        self.path = Path(str(db_path)).expanduser()
        if run_migrations:
            keeper.ensure_current(self.path)
        self._sessionmaker: async_sessionmaker[AsyncSession] = get_async_sessionmaker(self.path)

    def session(self) -> AsyncSession:
        """Open a new async session.

        Use as ``async with db.session() as s: ...`` — SA closes the
        session on exit and rolls back any uncommitted state.
        """
        return self._sessionmaker()
