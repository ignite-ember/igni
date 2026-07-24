"""Async Cypher client over the Neo4j Bolt driver.

One class lives here:

* :class:`Neo4jClient` — scoped to one commit's database
  (``neo4j`` default DB). Holds the open session, owns the
  write/read methods for items, chunks, edges, and per-commit
  metadata.

In the per-process model, each ``(project, commit)`` pair gets its
own Neo4j process. The client targets the default ``neo4j`` DB
within that process.

Shares the same ``neo4j.AsyncDriver`` with the runtime — one
driver per BE process, database selected per-session
``database=...`` parameters.

The driver is owned by the BE's :class:`Neo4jRuntime`; clients
take it as a constructor argument so the call site controls
lifecycle (drivers are heavy — one per process, not per query).

Cypher translation:

* Edges use a single ``[:REL {kind, meta}]`` label with an indexed
  ``kind`` property. This dodges the "Cypher can't parameterise
  rel-type" issue without string concat against user input —
  the ``Relation`` enum validates ``kind`` at the boundary, and
  the property lookup uses an indexed match.
* Vector search uses ``CALL db.index.vector.queryNodes`` with
  over-fetch (factor 4) and client-side re-rank — Neo4j 5.26
  doesn't expose a per-query ``ef`` knob, so we buy back recall
  by fetching more candidates than needed.
* Where-clause filters (the same shape
  :class:`ChromaWhereFilter` produces) translate to Cypher
  WHERE fragments via a small renderer. The translation is
  intentionally narrow — only the operators the agent-facing
  tool actually emits (``$eq``, ``$in``, ``$contains``, ``$gt``,
  ``$gte``, ``$lt``, ``$lte``) are wired.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from neo4j import AsyncDriver, AsyncSession

from ember_code.core.code_index.delta.ops import ReferenceMeta
from ember_code.core.code_index.enums import Relation
from ember_code.core.code_index.neo4j_codec import Neo4jRowCodec
from ember_code.core.code_index.neo4j_schema import (
    COMMIT_SCHEMA_STATEMENTS,
    DEFAULT_DATABASE,
    META_SCHEMA_STATEMENTS,
)
from ember_code.core.code_index.schema.commit_metadata import (
    CommitMetadataBulkCreate,
    CommitMetadataCreate,
    CommitMetadataEntry,
)
from ember_code.core.code_index.schema.file_reference import FileReference
from ember_code.core.code_index.schema.items import (
    CodeIndexItem,
    CodeIndexItemCreate,
    CodeIndexResult,
)
from ember_code.core.code_index.schema.stats import HeadStats

logger = logging.getLogger(__name__)


# Allowlist of :Item property names the where-renderer accepts. Property
# names are interpolated into Cypher (values are parameterized), so
# without this a caller-controlled key like ``x) OR true //`` would
# become executable query text. Derived from ``CodeIndexItemCreate``
# at import time so adding a new field is one line in the Pydantic
# model — and ``archived`` (an admin field on the node, not on the
# create schema) is included explicitly.
_ITEM_WHERE_FIELDS: frozenset[str] = frozenset(CodeIndexItemCreate.model_fields) | frozenset(
    {"archived"}
)


# Vector search over-fetch factor — matches today's chroma behaviour
# (limit * 4 candidates, re-rank by exact cosine if a follow-up
# step asks for it). Tunable via constructor for the recall test.
_DEFAULT_VECTOR_OVERFETCH = 4

# Hard cap on the parent-doc filter resolution (where-clause pre-
# filter). Mirrors :attr:`ChunkSearch.PARENT_ID_CAP`.
_DEFAULT_PARENT_ID_CAP = 10_000


class Neo4jClient:
    """One commit's database — items, chunks, edges, per-commit metadata.

    Construction is cheap (no I/O). The first method call opens a
    session against ``self._db_name``; the session is closed when
    :meth:`close` runs. The underlying ``AsyncDriver`` is owned by
    the caller (one driver per BE process).
    """

    def __init__(
        self,
        driver: AsyncDriver,
        project_id: str,
        commit_sha: str = "",
        *,
        vector_overfetch: int = _DEFAULT_VECTOR_OVERFETCH,
    ):
        self._driver = driver
        self._project_id = project_id
        # Per-process isolation: each Neo4j process = one commit.
        # ``commit_sha`` identifies which commit this client's process
        # holds — used for admin-facing calls (``drop_database``,
        # ``touch_commit``) and returned via the ``commit_sha``
        # property. Data queries don't filter on it (the process IS
        # the scope); ``project_hash`` scopes across projects sharing
        # a physical DB.
        self._commit_sha = commit_sha
        self._db_name = DEFAULT_DATABASE
        self._codec = Neo4jRowCodec()
        self._overfetch = vector_overfetch

    @property
    def database_name(self) -> str:
        return self._db_name

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def commit_sha(self) -> str:
        return self._commit_sha

    # ── Lifecycle ───────────────────────────────────────────────────

    async def ensure_database(self) -> None:
        """No-op in the single-DB design.

        Kept on the public surface for back-compat with
        callers that explicitly ``ensure_database()`` before
        applying schema. The default ``neo4j`` DB exists from
        install time on every Neo4j deployment; we don't need
        (and on Community Edition, *can't*) issue
        ``CREATE DATABASE``.
        """
        return

    async def apply_schema(self) -> None:
        """Apply the schema to the default ``neo4j`` DB.

        Idempotent — every statement uses ``IF NOT EXISTS`` so a
        partially-built DB picks up where it left off.
        """
        async with self.session() as session:
            for stmt in COMMIT_SCHEMA_STATEMENTS:
                await session.run(stmt)

    async def close(self) -> None:
        """No-op — the driver is shared; per-session closes happen
        via the ``async with self.session()`` blocks. Kept on
        the surface so callers can write symmetric ``async with``
        blocks without leaking the driver concern.
        """

    def session(self) -> AsyncSession:
        """Open a session bound to the default ``neo4j`` DB.

        Public so the service classes (:class:`FileReferenceService`,
        :class:`CommitMetadataService`) and tests can drive raw
        Cypher through the same driver without going through
        per-method wrappers.
        """
        return self._driver.session(database=self._db_name)

    async def execute_query(
        self,
        cypher: str,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Run arbitrary Cypher and return rows as plain dicts.

        The escape hatch for AI / callers that want to write
        custom Cypher against the data model — see
        :data:`ember_code.core.code_index.neo4j_schema.GRAPH_SCHEMA_DESCRIPTION`
        for the node/edge/property contract.

        AI is responsible for correctness: ``project_hash``
        scoping on every query, commit-scoping on edge reads,
        proper index usage. The typed methods
        (:meth:`get_item`, :meth:`vector_search`, :meth:`get_edges`,
        etc.) are safer — use them when they fit. This method
        is for cases the typed API doesn't cover.

        Returns a list of ``dict`` records. The keys are the
        Cypher aliases (e.g. ``"item"``, ``"score"``); values
        are passed through from the driver (nodes come back as
        ``neo4j._Node`` objects — call :func:`dict` on them
        inside the query, or use ``properties(node)`` in Cypher,
        to get plain dicts).
        """
        async with self.session() as session:
            result = await session.run(cypher, **params)
            records = (await result.to_eager_result()).records
        return [dict(r) for r in records]

    @classmethod
    async def drop_database(cls, driver: AsyncDriver, project_id: str, commit_sha: str) -> None:
        """Drop all nodes/edges for this commit (items, chunks, edges).

        In the per-process model the runtime handles process
        teardown; this cleans up any remaining nodes in the
        process DB before the data dir is deleted.
        """
        async with driver.session() as session:
            await session.execute_write(cls._drop_commit_tx, project_id)

    @staticmethod
    async def _drop_commit_tx(tx, project_id: str) -> None:
        """Tx body for :meth:`drop_database`."""
        # Detach-delete all nodes scoped to this project.
        await tx.run(
            "MATCH (i:Item {project_hash: $proj}) "
            "OPTIONAL MATCH (i)-[:HAS_CHUNK]->(c:Chunk) "
            "DETACH DELETE i, c",
            proj=project_id,
        )

    # ── Item writes ─────────────────────────────────────────────────

    async def upsert_item(
        self,
        item: CodeIndexItem,
        chunks: list[tuple[str, list[float]]],
    ) -> None:
        """Insert or replace an item and its chunk set, attached to this commit.

        ``chunks`` is a list of ``(chunk_text, embedding)`` tuples;
        chunk IDs are content-addressed (``f"{item.item_id}::{i}"``).
        Re-upserts replace the existing chunk set in this commit
        — if the same content is also in other commits, those
        commits' ``[:PRESENT_IN]`` edges are untouched.

        Item upsert is a ``MERGE`` (idempotent on ``item_id``) +
        ``SET i += $props``. The :Commit node is MERGE-created
        on first use; the ``[:PRESENT_IN]`` edge is MERGE-created
        so re-upserts in the same commit are idempotent.
        """
        props = self._codec.to_node_params(item, content=item.content)
        # Stamp project_hash on the node so project-scoped queries
        # (``i.project_hash = $proj``) work without a separate
        # commit-edge match. The codec doesn't know about
        # project_hash (it only sees the item); the client is
        # the one that knows the active project.
        props["project_hash"] = self._project_id
        async with self.session() as session:
            await session.execute_write(self._upsert_item_tx, item, props, chunks, self._project_id)

    @staticmethod
    async def _upsert_item_tx(
        tx, item: CodeIndexItem, props: dict, chunks: list, project_id: str
    ) -> None:
        # Per-process isolation: each Neo4j process holds exactly one
        # commit's data. No :Commit node needed — the process IS the
        # commit boundary. project_hash scopes items within a
        # multi-project runtime.
        #
        # 1. Upsert the :Item with all properties. Node key is
        # (item_id, project_hash) — unique within a process.
        await tx.run(
            "MERGE (i:Item {item_id: $item_id, project_hash: $proj}) SET i += $props",
            item_id=item.item_id,
            proj=project_id,
            props=props,
        )
        # 2. Replace the chunk set for this item.
        await tx.run(
            "MATCH (i:Item {item_id: $item_id, project_hash: $proj})"
            "-[:HAS_CHUNK]->(c:Chunk) "
            "DETACH DELETE c",
            item_id=item.item_id,
            proj=project_id,
        )
        # 3. Create the new chunks.
        for i, (text, embedding) in enumerate(chunks):
            chunk_id = f"{item.item_id}::{i}"
            chunk_props = {
                "chunk_id": chunk_id,
                "parent_id": item.item_id,
                "chunk_index": i,
                "text": text,
                "embedding": embedding,
                "name": props.get("name"),
                "type": props.get("type"),
                "kind": props.get("kind"),
                "path": props.get("path"),
                "file_extension": props.get("file_extension"),
                "repository_id": props.get("repository_id"),
            }
            await tx.run(
                "MATCH (i:Item {item_id: $item_id, project_hash: $proj}) "
                "MERGE (ch:Chunk {chunk_id: $chunk_id}) "
                "ON CREATE SET ch += $chunk_props "
                "ON MATCH SET ch += $chunk_props "
                "MERGE (i)-[:HAS_CHUNK]->(ch)",
                item_id=item.item_id,
                proj=project_id,
                chunk_id=chunk_id,
                chunk_props=chunk_props,
            )

    async def delete_item(self, item_id: str) -> None:
        """Drop the item's PRESENT_IN edge to this commit (and the chunks only-in this commit).

        Idempotent — deleting a missing edge is a no-op.

        In the relationship model, "delete the item from this
        commit" means: drop the ``[:PRESENT_IN]`` edge from the
        item to this commit. If the item has no other PRESENT_IN
        edges, the item (and its chunks) is also dropped. If it
        has edges to other commits, the item survives in those
        commits — only the local edge is removed.
        """
        async with self.session() as session:
            await session.execute_write(self._delete_item_tx, item_id, self._project_id)

    @staticmethod
    async def _delete_item_tx(tx, item_id: str, project_id: str) -> None:
        # DETACH DELETE handles ALL relationships (any type, any direction)
        # atomically before deleting the node. This avoids the issue where
        # separate MATCH/DELETE steps miss edges (e.g. when the item is
        # the TARGET of a directed edge and the MATCH pattern didn't match).
        await tx.run(
            "MATCH (i:Item {item_id: $item_id, project_hash: $proj}) DETACH DELETE i",
            item_id=item_id,
            proj=project_id,
        )

    # ── Reads ───────────────────────────────────────────────────────

    async def vector_search(
        self,
        embedding: list[float],
        where: dict[str, Any] | None = None,
        limit: int = 20,
        commit: str = "",
    ) -> list[CodeIndexResult]:
        """Cosine-similarity search over chunk embeddings in this process.

        Over-fetches by ``self._overfetch`` because Neo4j 5.26
        doesn't expose a query-time ``ef`` knob — we buy back
        recall by asking the index for ``limit * overfetch``
        candidates, then truncate to ``limit``.

        Per-process isolation: this process holds exactly one
        commit's data, so no commit scoping is needed in queries.

        ``commit`` is echoed into each result's ``commit`` field
        (session-derived, not stored on nodes).
        """
        async with self.session() as session:
            if where:
                candidate_ids = await self._resolve_parent_ids(session, where, self._project_id)
                if not candidate_ids:
                    return []
                return await self._vector_search_among(
                    session,
                    embedding,
                    candidate_ids,
                    limit,
                    self._project_id,
                    commit=commit,
                )
            return await self._vector_search_all(
                session, embedding, limit, self._project_id, commit=commit
            )

    async def _resolve_parent_ids(
        self,
        session: AsyncSession,
        where: dict[str, Any],
        project_id: str,
    ) -> list[str]:
        """Find parent Item IDs in this process matching a where filter."""
        where_clause, where_params = self._render_where(where, prefix="i")
        cypher = (
            f"MATCH (i:Item) "
            f"WHERE i.project_hash = $proj AND {where_clause} "
            f"RETURN i.item_id AS id LIMIT {_DEFAULT_PARENT_ID_CAP}"
        )
        result = await session.run(cypher, proj=project_id, **where_params)
        records = (await result.to_eager_result()).records
        return [r["id"] for r in records]

    async def _vector_search_all(
        self,
        session: AsyncSession,
        embedding: list[float],
        limit: int,
        project_id: str,
        commit: str = "",
    ) -> list[CodeIndexResult]:
        """Vector search across all chunks in this process."""
        n = max(limit * self._overfetch, limit)
        result = await session.run(
            "CALL db.index.vector.queryNodes('chunk_embedding', $n, $embedding) "
            "YIELD node AS chunk, score "
            "MATCH (item:Item {item_id: chunk.parent_id, project_hash: $proj})-[:HAS_CHUNK]->(chunk) "
            "RETURN item, chunk, score "
            "ORDER BY score DESC LIMIT $limit",
            n=n,
            embedding=embedding,
            limit=limit,
            proj=project_id,
        )
        return await self._collect_search_results(session, result, commit=commit)

    async def _vector_search_among(
        self,
        session: AsyncSession,
        embedding: list[float],
        candidate_ids: list[str],
        limit: int,
        project_id: str,
        commit: str = "",
    ) -> list[CodeIndexResult]:
        """Vector search restricted to a fixed set of parent item IDs."""
        n = max(limit * self._overfetch, limit)
        result = await session.run(
            "CALL db.index.vector.queryNodes('chunk_embedding', $n, $embedding) "
            "YIELD node AS chunk, score "
            "MATCH (item:Item {item_id: chunk.parent_id, project_hash: $proj})-[:HAS_CHUNK]->(chunk) "
            "WHERE chunk.parent_id IN $candidate_ids "
            "RETURN item, chunk, score "
            "ORDER BY score DESC LIMIT $limit",
            n=n,
            embedding=embedding,
            candidate_ids=candidate_ids,
            proj=project_id,
            limit=limit,
        )
        return await self._collect_search_results(session, result, commit=commit)

    async def _collect_search_results(
        self,
        session: AsyncSession,
        result,
        commit: str = "",
    ) -> list[CodeIndexResult]:
        """Dedupe by parent ID, hydrate the parent node, return results.

        Today's :class:`ChunkSearch._dedupe_by_parent` keeps the
        best-scoring chunk per parent. We do the same here.
        """
        records = (await result.to_eager_result()).records
        if not records:
            return []
        best_per_parent: dict[str, dict[str, Any]] = {}
        for record in records:
            chunk_node = record["chunk"]
            parent_id = chunk_node["parent_id"]
            score = float(record["score"])
            current = best_per_parent.get(parent_id)
            if current is None or score > current["score"]:
                best_per_parent[parent_id] = {
                    "score": score,
                    "chunk": chunk_node,
                    "item_node": record["item"],
                }
        if not best_per_parent:
            return []

        # Hydrate the parent Item nodes — fetch documents + metadata.
        parent_ids = list(best_per_parent.keys())
        parent_result = await session.run(
            "MATCH (i:Item) WHERE i.item_id IN $ids "
            "RETURN i.item_id AS id, i.content AS content, "
            "properties(i) AS props",
            ids=parent_ids,
        )
        parent_rows = {
            r["id"]: (r["content"] or "", dict(r["props"] or {}))
            for r in (await parent_result.to_eager_result()).records
        }

        out: list[CodeIndexResult] = []
        for parent_id, hit in best_per_parent.items():
            content, props = parent_rows.get(parent_id, ("", {}))
            preview = hit["chunk"].get("text") or ""
            if len(preview) > Neo4jChunkSearch.PREVIEW_MAX_CHARS:
                preview = preview[: Neo4jChunkSearch.PREVIEW_MAX_CHARS] + "..."
            out.append(
                self._codec.from_node(
                    parent_id,
                    props,
                    content=content,
                    score=hit["score"],
                    chunk_preview=preview,
                    commit=commit,
                )
            )
        out.sort(key=lambda r: r.score or 0.0, reverse=True)
        return out[: len(best_per_parent)]  # caller can slice further

    async def get_item(self, item_id: str, commit: str = "") -> CodeIndexResult | None:
        """Point read by item ID, scoped to this commit via (item_id, project_hash)."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (i:Item {item_id: $item_id, project_hash: $proj}) "
                "RETURN properties(i) AS props, i.content AS content",
                item_id=item_id,
                proj=self._project_id,
            )
            records = (await result.to_eager_result()).records
            if not records:
                return None
            props = dict(records[0]["props"] or {})
            content = records[0]["content"] or ""
            return self._codec.from_node(item_id, props, content=content, commit=commit)

    async def filter_items(
        self,
        where: dict[str, Any] | None = None,
        ids: list[str] | None = None,
        limit: int = 20,
        commit: str = "",
    ) -> list[CodeIndexResult]:
        """Direct fetch / filter against :Item in this commit (no semantic search).

        Mirrors :meth:`CodeIndex.filter_items`. Scoped to this
        commit via the process/DB boundary (each process holds exactly one commit).
        """
        clauses: list[str] = ["i.project_hash = $proj"]
        params: dict[str, Any] = {"limit": limit, "proj": self._project_id}
        if where:
            where_clause, where_params = self._render_where(where, prefix="i")
            clauses.append(where_clause)
            params.update(where_params)
        if ids is not None:
            clauses.append("i.item_id IN $ids")
            params["ids"] = list(ids)
        where_str = " AND ".join(clauses) if clauses else ""
        where_clause = f"WHERE {where_str} " if clauses else " "
        async with self.session() as session:
            result = await session.run(
                f"MATCH (i:Item) "
                f"{where_clause}"
                f"RETURN i.item_id AS id, i.content AS content, properties(i) AS props "
                f"ORDER BY i.item_id LIMIT $limit",
                **params,
            )
            records = (await result.to_eager_result()).records
            out: list[CodeIndexResult] = []
            for r in records:
                props = dict(r["props"] or {})
                out.append(
                    self._codec.from_node(
                        r["id"],
                        props,
                        content=r["content"] or "",
                        commit=commit,
                    )
                )
            return out

    async def get_all_items(self, limit: int = 100_000) -> list[CodeIndexResult]:
        """Return all items in this commit's DB (for carry-over).

        Used by ``_carryover_from_parent`` to read surviving items
        from the parent process before writing them to the child.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (i:Item) "
                "WHERE i.project_hash = $proj "
                "RETURN i.item_id AS id, i.content AS content, properties(i) AS props "
                "ORDER BY i.item_id LIMIT $limit",
                proj=self._project_id,
                limit=limit,
            )
            records = (await result.to_eager_result()).records
            return [
                self._codec.from_node(
                    r["id"],
                    dict(r["props"] or {}),
                    content=r["content"] or "",
                )
                for r in records
            ]

    async def get_all_edges(self) -> list[FileReference]:
        """Return all edges in this commit's DB (for carry-over).

        Used by ``_carryover_from_parent`` to read surviving edges
        from the parent process before writing them to the child.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Item)-[r:REL]->(b:Item) "
                "WHERE a.project_hash = $proj AND b.project_hash = $proj "
                "RETURN a.item_id AS from_id, b.item_id AS to_id, "
                "r.kind AS kind, r.meta AS meta",
                proj=self._project_id,
            )
            records = (await result.to_eager_result()).records
            return [
                FileReference(
                    from_uuid=r["from_id"],
                    to_uuid=r["to_id"],
                    relation=r["kind"] or "",
                    meta=dict(r["meta"] or {}),
                )
                for r in records
            ]

    async def upsert_items_batch(
        self,
        items: list[CodeIndexItem],
        chunks_map: dict[str, list[tuple[str, list[float]]]],
    ) -> None:
        """Bulk upsert items + their chunks in one transaction.

        Used by ``_carryover_from_parent`` to write surviving items
        from the parent to the child process efficiently.
        """
        async with self.session() as session:
            for item in items:
                props = self._codec.to_node_params(item, content=item.content)
                props["project_hash"] = self._project_id
                chunks = chunks_map.get(item.item_id, [])
                await session.execute_write(
                    self._upsert_item_tx, item, props, chunks, self._project_id
                )

    async def list_files(self, limit: int = 50_000) -> HeadStats:
        """Quick per-commit stats for the CodeIndex panel.

        Dedupe by path so a file with N entities inside is counted
        once. Scoped to this commit via ``[:PRESENT_IN]``.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (i:Item) "
                "WHERE i.type = 'file' "
                "RETURN i.path AS path, i.file_extension AS ext",
            )
            records = (await result.to_eager_result()).records
        files_indexed = 0
        languages: dict[str, int] = {}
        for r in records:
            path = r["path"]
            ext = r["ext"]
            if not path:
                continue
            files_indexed += 1
            if ext:
                languages[ext] = languages.get(ext, 0) + 1
        return HeadStats(files_indexed=files_indexed, languages_indexed=languages)

    # ── Edges (per-commit) ──────────────────────────────────────────

    async def create_edge(
        self,
        from_uuid: str,
        to_uuid: str,
        relation: str | Relation,
        meta: dict | ReferenceMeta,
    ) -> FileReference:
        """Upsert a reference edge. ``meta`` is replaced on conflict.

        The :class:`Relation` enum validates ``relation`` at the
        boundary; we cast to ``str`` before param binding.
        """
        relation_str = relation.value if isinstance(relation, Relation) else relation
        meta_dict = meta.model_dump() if hasattr(meta, "model_dump") else dict(meta)
        async with self.session() as session:
            await session.execute_write(
                self._create_edge_tx,
                from_uuid,
                to_uuid,
                relation_str,
                meta_dict,
            )
        return FileReference(
            from_uuid=from_uuid,
            to_uuid=to_uuid,
            relation=relation_str,
            meta=meta_dict,
        )

    @staticmethod
    async def _create_edge_tx(tx, from_uuid, to_uuid, relation, meta_dict) -> None:
        # ``meta`` is a dict — Neo4j property values are limited
        # to primitives + arrays, so we JSON-serialize it. The
        # chroma-era codec did the same; this is a property
        # constraint, not a new constraint we're imposing.
        meta_json = json.dumps(meta_dict)
        await tx.run(
            "MERGE (a:Item {item_id: $from}) "
            "MERGE (b:Item {item_id: $to}) "
            "MERGE (a)-[r:REL {kind: $kind}]->(b) "
            "SET r.meta_json = $meta_json",
            **{"from": from_uuid, "to": to_uuid, "kind": relation, "meta_json": meta_json},
        )

    async def get_edges(
        self,
        uuids: list[str],
        kinds: list[str] | None = None,
    ) -> list[FileReference]:
        """Return every edge whose ``from_uuid`` or ``to_uuid`` is in ``uuids``.

        When ``kinds`` is provided, narrows to edges with one of
        those ``kind`` values. An empty ``uuids`` with ``kinds``
        returns every edge of those kinds (admin-style "all
        relations" queries). Mirrors
        :meth:`FileReferenceService.get_by_uuids`.
        """
        if not uuids and not kinds:
            return []
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if uuids:
            clauses.append("a.item_id IN $ids OR b.item_id IN $ids")
            params["ids"] = list(uuids)
        if kinds:
            clauses.append("r.kind IN $kinds")
            params["kinds"] = list(kinds)
        where_str = " AND ".join(clauses) if clauses else "true"
        async with self.session() as session:
            result = await session.run(
                f"MATCH (a:Item)-[r:REL]->(b:Item) WHERE {where_str} "
                f"RETURN a.item_id AS from_id, b.item_id AS to_id, "
                f"r.kind AS kind, r.meta_json AS meta",
                **params,
            )
            records = (await result.to_eager_result()).records
        return [
            FileReference(
                from_uuid=r["from_id"],
                to_uuid=r["to_id"],
                relation=r["kind"],
                meta=json.loads(r["meta"]) if r["meta"] else {},
            )
            for r in records
        ]

    async def delete_edge(
        self,
        from_uuid: str,
        to_uuid: str,
        kind: str | None = None,
    ) -> None:
        """Drop one edge (kind set) or all edges between a pair (kind None)."""
        clauses = ["a.item_id = $from_id", "b.item_id = $to_id"]
        params: dict[str, Any] = {"from_id": from_uuid, "to_id": to_uuid}
        if kind is not None:
            clauses.append("r.kind = $kind")
            params["kind"] = kind
        where_str = " AND ".join(clauses)
        async with self.session() as session:
            await session.execute_write(self._delete_edge_tx, where_str, params)

    @staticmethod
    async def _delete_edge_tx(tx, where_str, params) -> None:
        await tx.run(
            f"MATCH (a:Item)-[r:REL]->(b:Item) WHERE {where_str} DELETE r",
            **params,
        )

    async def delete_edges_for(self, item_id: str) -> int:
        """Drop every edge incident to ``item_id``. Returns delete count."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Item {item_id: $id})-[r:REL]-() DELETE r RETURN count(r) AS n",
                id=item_id,
            )
            records = (await result.to_eager_result()).records
            return int(records[0]["n"]) if records else 0

    # ── Per-commit metadata ─────────────────────────────────────────

    async def set_meta(self, payload: CommitMetadataCreate) -> None:
        """Upsert one ``Item.meta[<key>] = <value>`` entry."""
        async with self.session() as session:
            await session.execute_write(self._set_meta_tx, payload)

    @staticmethod
    async def _set_meta_tx(tx, payload: CommitMetadataCreate) -> None:
        # ``Item.meta_json`` is a JSON-serialized string property.
        # We avoid the i.meta[$key] syntax (Neo4j 5.x Community
        # doesn't support parameter-indexed map updates without
        # APOC). Each ``Item`` has ONE ``meta_json`` blob that
        # the application parses when reading — fine for small,
        # commit-scoped metadata (the file_reference service uses
        # the same pattern for edge metadata).
        await tx.run(
            "MERGE (i:Item {item_id: $item_id}) SET i.meta_json = $meta_json",
            item_id=payload.item_id,
            meta_json=json.dumps({payload.key: payload.value}),
        )

    async def bulk_set_meta(self, bulk: CommitMetadataBulkCreate) -> None:
        """Upsert many ``Item.meta[<key>] = <value>`` rows in one tx."""
        if not bulk.items:
            return
        async with self.session() as session:
            await session.execute_write(self._bulk_set_meta_tx, bulk)

    @staticmethod
    async def _bulk_set_meta_tx(tx, bulk: CommitMetadataBulkCreate) -> None:
        # Bulk upsert: precompute each row's meta_json Python-side
        # (key + value). UNWIND + MERGE keeps it to one round-trip.
        rows = [
            {"item_id": it.item_id, "meta_json": json.dumps({bulk.key: it.value})}
            for it in bulk.items
        ]
        await tx.run(
            "UNWIND $rows AS row "
            "MERGE (i:Item {item_id: row.item_id}) "
            "SET i.meta_json = row.meta_json",
            rows=rows,
        )

    async def get_meta(
        self,
        item_ids: list[str],
        key: str,
    ) -> dict[str, CommitMetadataEntry]:
        """Read ``Item.meta[key]`` for each of ``item_ids``.

        Returns a map ``item_id → CommitMetadataEntry``. Missing
        items / missing keys are simply absent from the result;
        callers default-fill as needed.
        """
        if not item_ids:
            return {}
        async with self.session() as session:
            # Read the whole meta_json blob; parse and extract the
            # specific key. The map-indexing syntax (i.meta[$key])
            # isn't valid in Neo4j 5.x Community without APOC, so we
            # do the index resolution in Python.
            result = await session.run(
                "MATCH (i:Item) WHERE i.item_id IN $ids "
                "RETURN i.item_id AS item_id, i.meta_json AS blob",
                ids=list(item_ids),
            )
            records = (await result.to_eager_result()).records
        out: dict[str, CommitMetadataEntry] = {}
        for r in records:
            blob = r["blob"]
            if not blob:
                continue
            try:
                meta_dict = json.loads(blob)
            except (json.JSONDecodeError, TypeError):
                continue
            if key not in meta_dict:
                continue
            out[r["item_id"]] = CommitMetadataEntry(
                item_id=r["item_id"],
                commit_sha="",  # session-derived; set by caller
                key=key,
                value=dict(meta_dict[key]),
            )
        return out

    async def delete_meta_for_item(self, item_id: str) -> None:
        """Drop the ``meta_json`` blob on one item."""
        async with self.session() as session:
            await session.execute_write(self._delete_meta_for_item_tx, item_id)

    @staticmethod
    async def _delete_meta_for_item_tx(tx, item_id: str) -> None:
        await tx.run(
            "MATCH (i:Item {item_id: $id}) REMOVE i.meta_json",
            id=item_id,
        )

    # ── Where-clause renderer ──────────────────────────────────────

    @staticmethod
    def _render_where(
        where: dict[str, Any],
        *,
        prefix: str = "i",
        _param_counter: list[int] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Translate a chroma-style where dict to a Cypher WHERE fragment.

        Handles the operators the agent tool emits:
        ``$eq`` (default for bare values), ``$ne``, ``$in``,
        ``$contains``, ``$gt``, ``$gte``, ``$lt``, ``$lte``.
        Multi-value category fields (``vulnerabilities``, ...) are
        list properties on the node, so ``$contains`` checks set
        membership via ``ANY(x IN i.field WHERE x = $val)``.

        Also handles ``$and`` at the top level (from
        :meth:`ChromaWhereFilter.to_chroma_where`) by recursively
        processing each sub-clause and joining with AND.

        Returns ``(where_clause, params)``. ``where_clause`` does
        NOT include the leading ``WHERE`` keyword — the caller
        prepends it.
        """
        if _param_counter is None:
            _param_counter = [0]
        clauses: list[str] = []
        params: dict[str, Any] = {}

        # Top-level $and: recursively render each sub-clause.
        if "$and" in where:
            for sub in where["$and"]:
                sub_clause, sub_params = Neo4jClient._render_where(
                    sub, prefix=prefix, _param_counter=_param_counter
                )
                if sub_clause:
                    clauses.append(sub_clause)
                params.update(sub_params)
            return " AND ".join(clauses), params

        for field, value in where.items():
            # Field name is interpolated into Cypher as an identifier
            # (values are parameterized). Reject anything outside the
            # :Item property allowlist so a caller-controlled key
            # can't escape the WHERE clause.
            if field not in _ITEM_WHERE_FIELDS:
                raise ValueError(
                    f"unknown where field: {field!r}; allowed: {sorted(_ITEM_WHERE_FIELDS)}"
                )
            idx = _param_counter[0]
            _param_counter[0] += 1
            param = f"w{idx}"
            if isinstance(value, dict):
                # Operator-bearing fragment
                for op, operand in value.items():
                    if op == "$eq":
                        clauses.append(f"{prefix}.{field} = ${param}_eq")
                        params[f"{param}_eq"] = operand
                    elif op == "$ne":
                        clauses.append(
                            f"({prefix}.{field} IS NULL OR {prefix}.{field} <> ${param}_ne)"
                        )
                        params[f"{param}_ne"] = operand
                    elif op == "$in":
                        clauses.append(f"{prefix}.{field} IN ${param}_in")
                        params[f"{param}_in"] = list(operand)
                    elif op == "$nin":
                        clauses.append(
                            f"({prefix}.{field} IS NULL OR NOT {prefix}.{field} IN ${param}_nin)"
                        )
                        params[f"{param}_nin"] = list(operand)
                    elif op == "$contains":
                        # List-membership check for multi-value
                        # categories. For scalars (e.g. ``path``)
                        # this falls back to substring match.
                        clauses.append(
                            f"ANY(x IN coalesce({prefix}.{field}, []) WHERE x = ${param}_c) "
                            f"OR {prefix}.{field} CONTAINS ${param}_c"
                        )
                        params[f"{param}_c"] = str(operand)
                    elif op in ("$gt", "$gte", "$lt", "$lte"):
                        op_map = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}
                        clauses.append(f"{prefix}.{field} {op_map[op]} ${param}_{op[1:]}")
                        params[f"{param}_{op[1:]}"] = operand
                    else:
                        raise ValueError(f"unsupported where operator: {op}")
            else:
                clauses.append(f"{prefix}.{field} = ${param}_eq")
                params[f"{param}_eq"] = value
        return " AND ".join(clauses), params


class Neo4jChunkSearch:
    """Semantic chunk search — kept as a class-level constant bag.

    Mirrors the constant surface of :class:`ChunkSearch` so callers
    that imported those constants keep working without a code
    change. The search logic itself lives on :class:`Neo4jClient`
    (above); this class exists purely for the constant namespace.
    """

    PREVIEW_MAX_CHARS = 1000
    PARENT_ID_CAP = _DEFAULT_PARENT_ID_CAP


class Neo4jMetaClient:
    """Per-project admin store: HEAD sha, branch pins, tracked commits.

    Unlike :class:`Neo4jClient` (which is scoped to one commit's
    process), the meta client holds project-level bookkeeping that
    must outlive any single commit process:

    * ``head`` — the current commit sha for the project.
    * ``branch_pins`` — commits exempt from retention sweeps.
    * ``:Commit`` nodes — one per tracked commit, carrying
      ``created_at`` / ``last_used_at`` so a retention sweep can
      drop idle, branch-unpinned commit DBs.

    All nodes are scoped by ``project_hash`` so one physical DB can
    hold many projects' admin data without leakage. The client takes
    the shared driver (owned by :class:`Neo4jRuntime`); construction
    is cheap and does no I/O.
    """

    _HEAD_KEY = "head"
    _BRANCH_PINS_KEY = "branch_pins"

    def __init__(self, driver: AsyncDriver, project_id: str):
        self._driver = driver
        self._project_id = project_id
        self._db_name = DEFAULT_DATABASE

    @property
    def project_id(self) -> str:
        return self._project_id

    def session(self) -> AsyncSession:
        """Open a session bound to the meta DB (the default ``neo4j``)."""
        return self._driver.session(database=self._db_name)

    async def apply_schema(self) -> None:
        """Create the meta indexes. Idempotent (``IF NOT EXISTS``)."""
        async with self.session() as session:
            for stmt in META_SCHEMA_STATEMENTS:
                await session.run(stmt)

    async def close(self) -> None:
        """No-op — the driver is shared and owned by the runtime."""

    # ── HEAD pointer ────────────────────────────────────────────────

    async def set_head(self, sha: str) -> None:
        """Set the project's current HEAD commit sha."""
        await self._set_meta(self._HEAD_KEY, sha)

    async def get_head(self) -> str | None:
        """Return the project's HEAD sha, or ``None`` if never set."""
        value = await self._get_meta(self._HEAD_KEY)
        return value if isinstance(value, str) else None

    # ── Branch pins ─────────────────────────────────────────────────

    async def set_branch_pins(self, shas: list[str]) -> None:
        """Replace the set of branch-pinned commit shas.

        Stored as a JSON array on the ``branch_pins`` :Meta node —
        Neo4j property values must be primitives or arrays of a
        single primitive type, and a JSON string keeps the read/write
        symmetric with the rest of the codec.
        """
        await self._set_meta(self._BRANCH_PINS_KEY, json.dumps(list(shas)))

    async def get_branch_pins(self) -> list[str]:
        """Return the branch-pinned shas (empty list if never set)."""
        value = await self._get_meta(self._BRANCH_PINS_KEY)
        if not value:
            return []
        return list(json.loads(value))

    # ── Commit tracking / retention ─────────────────────────────────

    async def touch_commit(self, sha: str, *, now_iso: str) -> None:
        """Upsert a :Commit node, stamping ``last_used_at``.

        ``created_at`` is set once (on first touch) and preserved on
        subsequent touches; ``last_used_at`` moves forward every time.
        The caller passes ``now_iso`` (rather than the client reading
        the clock) so the timestamp source stays testable and the
        function stays pure w.r.t. wall-clock.
        """
        async with self.session() as session:
            await session.run(
                "MERGE (c:Commit {project_hash: $proj, sha: $sha}) "
                "ON CREATE SET c.created_at = $now "
                "SET c.last_used_at = $now",
                proj=self._project_id,
                sha=sha,
                now=now_iso,
            )

    async def list_tracked_commits(self) -> list[str]:
        """Return every tracked commit sha for this project."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (c:Commit {project_hash: $proj}) RETURN c.sha AS sha",
                proj=self._project_id,
            )
            records = (await result.to_eager_result()).records
        return [r["sha"] for r in records]

    # ── Internals ───────────────────────────────────────────────────

    async def _set_meta(self, key: str, value: str) -> None:
        async with self.session() as session:
            await session.run(
                "MERGE (m:Meta {project_hash: $proj, key: $key}) SET m.value = $value",
                proj=self._project_id,
                key=key,
                value=value,
            )

    async def _get_meta(self, key: str) -> str | None:
        async with self.session() as session:
            result = await session.run(
                "MATCH (m:Meta {project_hash: $proj, key: $key}) RETURN m.value AS value",
                proj=self._project_id,
                key=key,
            )
            record = await result.single()
        return record["value"] if record else None
