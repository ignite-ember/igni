"""Agno's tables, made by the admin role (``igni migrate``).

Apart from :mod:`ember_code.core.db.rls` so that granting a producer its
role (``python -m ember_code.core.db.admin_cli grant-producer``) does not import Agno.
"""

from __future__ import annotations

import asyncio

from agno.db.postgres import AsyncPostgresDb

from ember_code.core.db.urls import DatabaseUrl


class AgnoTables:
    """Agno's tables, created by the admin role before any producer connects.

    A producer's role cannot create tables, so Agno is told not to
    (``create_schema=False``) at run time, and ``igni migrate`` makes them.
    """

    def __init__(self, admin: DatabaseUrl, *, session_table: str, memory_table: str) -> None:
        self._admin = admin
        self._session_table = session_table
        self._memory_table = memory_table

    def prepare(self) -> None:
        asyncio.run(self._prepare())

    async def _prepare(self) -> None:
        db = AsyncPostgresDb(
            db_url=self._admin.async_,
            session_table=self._session_table,
            memory_table=self._memory_table,
        )
        try:
            # Agno offers no public "create the tables" call; this is
            # the one it runs itself when it first needs them.
            await db._create_all_tables()
        finally:
            await db.close()


__all__ = ["AgnoTables"]
