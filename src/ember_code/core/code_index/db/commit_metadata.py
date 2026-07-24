"""Commit-scoped metadata persistence for indexed items.

Upserts, reads, and deletes for ``code_index_commit_metadata`` — facts
that would shift as the codebase evolves without a re-index (notably
``line_range`` spans across commits). One SQLite file per project gives
tenancy without a tenant column. Wire formats live in
``schema/commit_metadata.py``.

Read and delete paths share the model and handle; splitting them into
``CommitMetadataReader`` / ``CommitMetadataWriter`` would force DI
plumbing changes for marginal benefit at this size.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from ember_code.core.code_index.db.models import CommitMetadataModel
from ember_code.core.code_index.schema.commit_metadata import (
    CommitMetadataBulkCreate,
    CommitMetadataCreate,
    CommitMetadataEntry,
)
from ember_code.core.db.database import Database


class CommitMetadataService:
    def __init__(self, db: Database):
        self.db = db

    # -- Writes ----------------------------------------------------------------

    async def create_or_update(self, payload: CommitMetadataCreate) -> None:
        """Upsert one row keyed on ``(item_id, commit_sha, key)``."""
        stmt = insert(CommitMetadataModel).values(**payload.model_dump())
        stmt = stmt.on_conflict_do_update(
            index_elements=["item_id", "commit_sha", "key"],
            set_={"value": stmt.excluded.value},
        )
        async with self.db.session() as session, session.begin():
            await session.execute(stmt)

    async def bulk_create_or_update(self, bulk: CommitMetadataBulkCreate) -> None:
        """Upsert many rows in one statement. ``commit_sha`` and ``key`` are fixed."""
        if not bulk.items:
            return
        rows = [
            {
                "item_id": item.item_id,
                "commit_sha": bulk.commit_sha,
                "key": bulk.key,
                "value": item.value,
            }
            for item in bulk.items
        ]
        stmt = insert(CommitMetadataModel).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["item_id", "commit_sha", "key"],
            set_={"value": stmt.excluded.value},
        )
        async with self.db.session() as session, session.begin():
            await session.execute(stmt)

    # -- Reads -----------------------------------------------------------------

    async def get_by_items_and_commit(
        self,
        item_ids: list[str],
        commit_sha: str,
        key: str,
    ) -> dict[str, CommitMetadataEntry]:
        """Fetch entries for ``key`` at ``commit_sha`` for any of ``item_ids``."""
        if not item_ids:
            return {}
        async with self.db.session() as session:
            result = await session.execute(
                select(
                    CommitMetadataModel.item_id,
                    CommitMetadataModel.commit_sha,
                    CommitMetadataModel.key,
                    CommitMetadataModel.value,
                ).where(
                    CommitMetadataModel.commit_sha == commit_sha,
                    CommitMetadataModel.key == key,
                    CommitMetadataModel.item_id.in_(item_ids),
                )
            )
            rows = result.all()
        return {
            row.item_id: CommitMetadataEntry(
                item_id=row.item_id,
                commit_sha=row.commit_sha,
                key=row.key,
                value=dict(row.value or {}),
            )
            for row in rows
        }

    # -- Deletes ---------------------------------------------------------------

    async def delete_by_item(self, item_id: str) -> None:
        """Drop every commit-scoped row attached to ``item_id``."""
        async with self.db.session() as session, session.begin():
            await session.execute(
                delete(CommitMetadataModel).where(CommitMetadataModel.item_id == item_id)
            )

    async def delete_by_commit(self, commit_sha: str) -> None:
        """Drop every row scoped to ``commit_sha`` across all items/keys."""
        async with self.db.session() as session, session.begin():
            await session.execute(
                delete(CommitMetadataModel).where(CommitMetadataModel.commit_sha == commit_sha)
            )
