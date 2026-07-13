"""Commit-scoped metadata service backed by per-project SQLite via SQLAlchemy.

No tenant column — each project has its own ``state.db`` file.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from ember_code.core.code_index.db.models import CommitMetadataModel
from ember_code.core.db.database import Database


class CommitMetadataService:
    def __init__(self, db: Database):
        self.db = db

    async def create_or_update(
        self,
        item_id: str,
        commit_sha: str,
        key: str,
        value: dict,
    ) -> None:
        stmt = insert(CommitMetadataModel).values(
            item_id=item_id, commit_sha=commit_sha, key=key, value=value
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["item_id", "commit_sha", "key"],
            set_={"value": stmt.excluded.value},
        )
        async with self.db.session() as session, session.begin():
            await session.execute(stmt)

    async def bulk_create_or_update(
        self,
        commit_sha: str,
        key: str,
        items: list[dict[str, Any]],
    ) -> None:
        if not items:
            return
        rows = [
            {
                "item_id": item["item_id"],
                "commit_sha": commit_sha,
                "key": key,
                "value": item["value"],
            }
            for item in items
        ]
        stmt = insert(CommitMetadataModel).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["item_id", "commit_sha", "key"],
            set_={"value": stmt.excluded.value},
        )
        async with self.db.session() as session, session.begin():
            await session.execute(stmt)

    async def get_by_items_and_commit(
        self,
        item_ids: list[str],
        commit_sha: str,
        key: str,
    ) -> dict[str, dict]:
        if not item_ids:
            return {}
        async with self.db.session() as session:
            result = await session.execute(
                select(CommitMetadataModel.item_id, CommitMetadataModel.value).where(
                    CommitMetadataModel.commit_sha == commit_sha,
                    CommitMetadataModel.key == key,
                    CommitMetadataModel.item_id.in_(item_ids),
                )
            )
            rows = result.all()
        return {item_id: dict(value or {}) for item_id, value in rows}

    async def delete_by_item(self, item_id: str) -> None:
        async with self.db.session() as session, session.begin():
            await session.execute(
                delete(CommitMetadataModel).where(CommitMetadataModel.item_id == item_id)
            )

    async def delete_by_commit(self, commit_sha: str) -> None:
        async with self.db.session() as session, session.begin():
            await session.execute(
                delete(CommitMetadataModel).where(CommitMetadataModel.commit_sha == commit_sha)
            )
