"""File-to-file reference service backed by per-project SQLite via SQLAlchemy.

The relation kind is a first-class indexed column, so filter-by-relation
queries are real B-tree lookups.
"""

from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert

from ember_code.core.code_index.db.models import FileReferenceModel
from ember_code.core.code_index.schema.file_reference import FileReference
from ember_code.core.db.database import Database


class FileReferenceService:
    def __init__(self, db: Database):
        self.db = db

    # -- Reads -----------------------------------------------------------------

    async def get(self, from_uuid: str, to_uuid: str, relation: str) -> FileReference | None:
        async with self.db.session() as session:
            row = await session.get(FileReferenceModel, (from_uuid, to_uuid, relation))
            if row is None:
                return None
            return _row_to_reference(row)

    async def exists(self, from_uuid: str, to_uuid: str, relation: str) -> bool:
        async with self.db.session() as session:
            result = await session.execute(
                select(FileReferenceModel.from_uuid).where(
                    FileReferenceModel.from_uuid == from_uuid,
                    FileReferenceModel.to_uuid == to_uuid,
                    FileReferenceModel.relation == relation,
                )
            )
        return result.first() is not None

    async def get_by_uuids(
        self,
        uuids: list[str],
        *,
        relations: list[str] | None = None,
    ) -> list[FileReference]:
        """Return every edge whose ``from_uuid`` or ``to_uuid`` is in ``uuids``.

        When ``relations`` is provided, narrows to edges with one of
        those relation kinds. The ``relation`` column is indexed, so
        this stays cheap even for items with many edges.
        """
        if not uuids:
            return []
        async with self.db.session() as session:
            stmt = select(FileReferenceModel).where(
                or_(
                    FileReferenceModel.from_uuid.in_(uuids),
                    FileReferenceModel.to_uuid.in_(uuids),
                )
            )
            if relations:
                stmt = stmt.where(FileReferenceModel.relation.in_(relations))
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [_row_to_reference(r) for r in rows]

    async def query_by_relation(self, relation: str) -> list[FileReference]:
        """All edges of one relation kind. Direct index lookup."""
        async with self.db.session() as session:
            result = await session.execute(
                select(FileReferenceModel).where(FileReferenceModel.relation == relation)
            )
            return [_row_to_reference(r) for r in result.scalars().all()]

    # -- Writes ----------------------------------------------------------------

    async def create(
        self,
        from_uuid: str,
        to_uuid: str,
        relation: str,
        meta: dict,
    ) -> FileReference:
        """Upsert a reference. ``meta`` replaced; ``(from, to, relation)`` is the key."""
        stmt = insert(FileReferenceModel).values(
            from_uuid=from_uuid,
            to_uuid=to_uuid,
            relation=relation,
            meta=meta,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["from_uuid", "to_uuid", "relation"],
            set_={"meta": stmt.excluded.meta},
        )
        async with self.db.session() as session, session.begin():
            await session.execute(stmt)
        return FileReference(from_uuid=from_uuid, to_uuid=to_uuid, relation=relation, meta=meta)

    async def delete(self, from_uuid: str, to_uuid: str, relation: str | None = None) -> None:
        """Drop one edge (relation set) or all edges between a pair (relation None)."""
        async with self.db.session() as session, session.begin():
            stmt = delete(FileReferenceModel).where(
                FileReferenceModel.from_uuid == from_uuid,
                FileReferenceModel.to_uuid == to_uuid,
            )
            if relation is not None:
                stmt = stmt.where(FileReferenceModel.relation == relation)
            await session.execute(stmt)

    async def delete_by_uuid(self, uuid: str) -> int:
        """Drop all references involving ``uuid`` (called when an item is deleted)."""
        async with self.db.session() as session, session.begin():
            result = await session.execute(
                delete(FileReferenceModel).where(
                    or_(
                        FileReferenceModel.from_uuid == uuid,
                        FileReferenceModel.to_uuid == uuid,
                    )
                )
            )
            return result.rowcount or 0


# -- Internals ----------------------------------------------------------------


def _row_to_reference(row: FileReferenceModel) -> FileReference:
    return FileReference(
        from_uuid=row.from_uuid,
        to_uuid=row.to_uuid,
        relation=row.relation,
        meta=dict(row.meta or {}),
    )
