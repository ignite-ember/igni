"""Agno's tables, made by the admin role (``igni migrate``).

Apart from :mod:`igni_cloud.db.rls` so that granting a producer its
role (``python -m igni_cloud.db.admin_cli grant-producer``) does not import Agno.
"""

from __future__ import annotations

import asyncio

from agno.db.migrations.manager import MigrationManager
from agno.exceptions import MigrationRequiredError
from agno.db.postgres import AsyncPostgresDb

from ember_code.core.db.agno_schema import AgnoSchema
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
            # Tables an earlier Agno made are brought to the current schema
            # first: Agno 3 refuses to open one it would have made otherwise
            # (``MigrationRequiredError``), and moved a session's runs into a
            # table of their own (``ember_code.core.db.agno_schema`` does the
            # same on the desktop).
            try:
                await db._create_all_tables()
            except MigrationRequiredError:
                await MigrationManager(db).up()
                await db._create_all_tables()
            migrations = MigrationManager(db)
            for table_type in AgnoSchema.TABLE_TYPES:
                await migrations.up(table_type=table_type)
            await AgnoSchema.retire_legacy_runs(db, self._session_table)
        finally:
            await db.close()


__all__ = ["AgnoTables"]
