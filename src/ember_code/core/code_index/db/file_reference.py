"""File-to-file reference service.

Backend-pluggable: a :class:`FileReferenceService` wraps either a
SQLite :class:`Database` (the legacy default) or a :class:`Neo4jClient`
(the post-migration default). Methods dispatch on the backend type
so the same public surface works against either store. The SQLite
path is a straight B-tree on ``(from_uuid, to_uuid, relation)``; the
neo4j path is a ``(:Item)-[:REL {kind, meta_json}]->(:Item)`` edge.
"""

from __future__ import annotations

from typing import Union

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert

from ember_code.core.code_index.db.models import FileReferenceModel
from ember_code.core.code_index.delta.ops import ReferenceMeta
from ember_code.core.code_index.enums import Relation
from ember_code.core.code_index.schema.file_reference import FileReference
from ember_code.core.db.database import Database

# Imported under a type-only alias so the runtime import doesn't pull
# the neo4j driver into a non-neo4j-using install.
try:  # pragma: no cover - exercised when neo4j is installed
    from ember_code.core.code_index.neo4j_client import Neo4jClient
except ImportError:  # pragma: no cover
    Neo4jClient = None  # type: ignore[assignment,misc]


# Backend-agnostic type alias for the constructor.
_AnyBackend = Union[Database, "Neo4jClient"]


class FileReferenceService:
    def __init__(self, backend: _AnyBackend):
        self._backend = backend
        self._is_neo4j = Neo4jClient is not None and isinstance(backend, Neo4jClient)

    # -- Reads -----------------------------------------------------------------

    async def get(self, from_uuid: str, to_uuid: str, relation: str) -> FileReference | None:
        if self._is_neo4j:
            edges = await self._backend.get_edges([from_uuid, to_uuid], kinds=[relation])
            for e in edges:
                if e.from_uuid == from_uuid and e.to_uuid == to_uuid and e.relation == relation:
                    return e
            return None
        async with self._backend.session() as session:
            row = await session.get(FileReferenceModel, (from_uuid, to_uuid, relation))
            if row is None:
                return None
            return _row_to_reference(row)

    async def exists(self, from_uuid: str, to_uuid: str, relation: str) -> bool:
        return await self.get(from_uuid, to_uuid, relation) is not None

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
        if self._is_neo4j:
            return await self._backend.get_edges(uuids, kinds=relations)
        async with self._backend.session() as session:
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
        if self._is_neo4j:
            # No ``from_uuid`` constraint → fetch everything matching
            # this kind. Over-fetch into a single uuid set so the
            # existing get_edges path works without a new method.
            return await self._backend.get_edges([], kinds=[relation])
        async with self._backend.session() as session:
            result = await session.execute(
                select(FileReferenceModel).where(FileReferenceModel.relation == relation)
            )
            return [_row_to_reference(r) for r in result.scalars().all()]

    # -- Writes ----------------------------------------------------------------

    async def create(
        self,
        from_uuid: str,
        to_uuid: str,
        relation: str | Relation,
        meta: dict | ReferenceMeta,
    ) -> FileReference:
        """Upsert a reference. ``meta`` replaced; ``(from, to, relation)`` is the key.

        Accepts ``relation`` as a raw string OR a :class:`Relation` enum
        member. Accepts ``meta`` as either a raw dict or a
        :class:`ReferenceMeta` instance.
        """
        if self._is_neo4j:
            return await self._backend.create_edge(from_uuid, to_uuid, relation, meta)
        relation_str = relation.value if isinstance(relation, Relation) else relation
        meta_dict = meta.model_dump() if hasattr(meta, "model_dump") else dict(meta)
        stmt = insert(FileReferenceModel).values(
            from_uuid=from_uuid,
            to_uuid=to_uuid,
            relation=relation_str,
            meta=meta_dict,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["from_uuid", "to_uuid", "relation"],
            set_={"meta": stmt.excluded.meta},
        )
        async with self._backend.session() as session, session.begin():
            await session.execute(stmt)
        return FileReference(
            from_uuid=from_uuid, to_uuid=to_uuid, relation=relation_str, meta=meta_dict
        )

    async def delete(self, from_uuid: str, to_uuid: str, relation: str | None = None) -> None:
        """Drop one edge (relation set) or all edges between a pair (relation None)."""
        if self._is_neo4j:
            await self._backend.delete_edge(from_uuid, to_uuid, kind=relation)
            return
        async with self._backend.session() as session, session.begin():
            stmt = delete(FileReferenceModel).where(
                FileReferenceModel.from_uuid == from_uuid,
                FileReferenceModel.to_uuid == to_uuid,
            )
            if relation is not None:
                stmt = stmt.where(FileReferenceModel.relation == relation)
            await session.execute(stmt)

    async def delete_by_uuid(self, uuid: str) -> int:
        """Drop all references involving ``uuid`` (called when an item is deleted)."""
        if self._is_neo4j:
            return await self._backend.delete_edges_for(uuid)
        async with self._backend.session() as session, session.begin():
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
