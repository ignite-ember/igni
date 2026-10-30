"""PostgreSQL as a shared database for igni (``igni.storage`` entry point ``postgresql``).

:class:`~ember_code.core.db.providers.SharedStorage` for PostgreSQL:
Agno's storage kept to one scope's rows (:class:`ScopedAsyncPostgresDb`),
uploads as large objects (:class:`LargeObjectAttachments`), and what an
admin's ``igni migrate`` prepares besides igni's tables — Agno's tables,
made by the admin role since a producer's role may not create any, and
row-level security on every table (:mod:`igni_cloud.db.rls`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ember_code.core.db.database import Database
from ember_code.core.db.engine import cache
from ember_code.core.db.providers import SharedStorage
from ember_code.core.db.scope import StorageScope
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.memory.manager import StorageManager
from igni_cloud.db.agno_tables import AgnoTables
from igni_cloud.db.rls import RowLevelSecurity
from igni_cloud.storage.attachments import LargeObjectAttachments
from igni_cloud.storage.scoped_agno import ScopedAsyncPostgresDb


class PostgresStorage(SharedStorage):
    def agno_db(
        self,
        *,
        url: DatabaseUrl,
        scope: StorageScope,
        session_table: str,
        memory_table: str,
    ) -> Any:
        return ScopedAsyncPostgresDb(
            scope=scope,
            db_engine=cache.get_async_engine(url),
            # ``igni migrate`` made Agno's tables as the admin role: a
            # producer's role may not create any.
            create_schema=False,
            session_table=session_table,
            memory_table=memory_table,
        )

    def attachments(self, db: Database, project_dir: Path) -> Any:
        return LargeObjectAttachments(db, project_dir)

    def prepare(self, admin: DatabaseUrl) -> None:
        AgnoTables(
            admin,
            session_table=StorageManager.SESSION_TABLE,
            memory_table=StorageManager.MEMORY_TABLE,
        ).prepare()
        RowLevelSecurity(admin).apply()


__all__ = ["PostgresStorage"]
