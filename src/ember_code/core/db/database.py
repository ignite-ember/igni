"""Async database handle backed by a single SQLite file.

Each instance owns one SQLite file (per-project ``state.db`` for project
state, or the global ``ember.db`` for memory + learning). Construction
runs ``alembic upgrade head`` synchronously to ensure the schema matches
the ORM models — alembic is sync-only and the upgrade is a one-shot per
file per process.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ember_code.core.db.engine import get_async_sessionmaker
from ember_code.core.db.migrations import upgrade_to_head


class Database:
    def __init__(self, db_path: str | Path, *, run_migrations: bool = True):
        self.path = Path(str(db_path)).expanduser()
        if run_migrations:
            upgrade_to_head(self.path)
        self._sessionmaker: async_sessionmaker[AsyncSession] = get_async_sessionmaker(self.path)

    def session(self) -> AsyncSession:
        """Open a new async session.

        Use as ``async with db.session() as s: ...`` — SA closes the
        session on exit and rolls back any uncommitted state.
        """
        return self._sessionmaker()
