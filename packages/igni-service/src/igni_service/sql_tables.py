"""Creating the SDK's tables on first use, safely alongside other replicas."""

from __future__ import annotations

import asyncio

from sqlalchemy import Table
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from igni_service.tables import IgniTables


class TableMaker:
    """Makes some of `IgniTables` exist, once per process."""

    def __init__(self, engine: AsyncEngine, tables: list[Table], *, create: bool = True) -> None:
        self._engine = engine
        self._tables = tables
        self._ready = not create
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            try:
                async with self._engine.begin() as connection:
                    await connection.run_sync(
                        IgniTables.metadata.create_all, tables=self._tables, checkfirst=True
                    )
            except DBAPIError:
                # Two replicas creating a table at once: one loses the race
                # inside the database's catalogue. The tables exist either
                # way, which is checked rather than assumed.
                async with self._engine.connect() as connection:
                    missing = await connection.run_sync(
                        lambda sync: [
                            t.name for t in self._tables if not sync.dialect.has_table(sync, t.name)
                        ]
                    )
                if missing:
                    raise
            self._ready = True
