"""The key store for a deployment: one row in the service's own database.

**The table is the SDK's.** `igni_service_key`, one row, on
`IgniTables.metadata` (`igni_service.tables`). Either `SqlKeyStore`
creates it on first use (tolerant of a replica creating it at the same
moment), or the service owns it in migrations and passes
`create_table=False`.

**Replicas starting together** each generate a key and `claim` it. The
insert is plain: the primary key lets exactly one through, the others get
an integrity error, and every replica then reads the row that won. No
dialect-specific upsert, so it works the same on Postgres, MySQL and
SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from igni_service.errors import KeyStoreError
from igni_service.keystore import KeyCipher, StoredKey
from igni_service.sql_tables import TableMaker
from igni_service.tables import IgniTables

#: The one row's primary key.
THE_KEY = "service"


class SqlKeyStore:
    """Keeps the service's key sealed in one row of `igni_service_key`."""

    #: Include in Alembic's `target_metadata` to own the table in migrations.
    metadata = IgniTables.metadata
    table = IgniTables.service_key

    def __init__(
        self, engine: AsyncEngine, cipher: KeyCipher, *, create_table: bool = True
    ) -> None:
        self._engine = engine
        self._cipher = cipher
        self._tables = TableMaker(engine, [self.table], create=create_table)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def get(self) -> StoredKey | None:
        await self._tables.ensure()
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(self.table.c.kid, self.table.c.private_key_encrypted).where(
                        self.table.c.name == THE_KEY
                    )
                )
            ).first()
        if row is None:
            return None
        return StoredKey(
            kid=str(row.kid), private_pem=self._cipher.open(str(row.private_key_encrypted))
        )

    async def claim(self, key: StoredKey) -> StoredKey:
        await self._tables.ensure()
        sealed = self._cipher.seal(key.private_pem)
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    insert(self.table).values(
                        name=THE_KEY,
                        kid=key.kid,
                        private_key_encrypted=sealed,
                        created_at=datetime.now(UTC),
                    )
                )
        except IntegrityError:
            # Another replica kept its key first. Theirs is the identity.
            pass
        kept = await self.get()
        if kept is None:
            raise KeyStoreError("the service key was written and then not found")
        return kept
