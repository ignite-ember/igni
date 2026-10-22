"""Async Cypher client for the per-project artifact store.

One class lives here: :class:`Neo4jArtifactClient`, scoped to one
project's database. The driver is owned by the BE's ``Neo4jRuntime``
(``driver_for_artifacts(project_hash)`` — drivers are heavy, one per
process, not per query), so this client takes it as a constructor
argument and never closes it.

This store is **per-project**, shared across commits, unlike the code
index, which is per-``(project, commit)``. It also applies its own
DDL: the store it replaces re-used ``COMMIT_SCHEMA_STATEMENTS``, the
code index's schema, in a database that has no ``:Item`` and no
``:Commit``.

Cypher notes:

* **Writes are one transaction, several statements.** A transaction
  reads its own writes, so the version created by the first statement
  is visible to the edge statement that follows. They are split
  because ``UNWIND`` over an empty list drops the row — chaining
  chunks and edges into one statement would silently skip both
  whenever a version has no chunks.
* **Vector search over-fetches.** ``db.index.vector.queryNodes``
  ranks before our ``head`` filter can run and Neo4j 5.26 exposes no
  per-query ``ef``, so we ask for ``limit * overfetch`` candidates and
  re-rank after filtering. Without the over-fetch, an artifact with a
  long revision history crowds its own superseded versions out of the
  result set and the caller gets fewer hits than it asked for.
* **Staleness is a query, not a flag.** Nothing is written when a
  source moves. The alternative — marking dependents dirty on every
  write — needs the write path to fan out across the dependency graph
  and leaves flags to be repaired when it half-fails.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from neo4j import AsyncDriver, AsyncSession

from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import (
    ArtifactLineage,
    ArtifactSearchHit,
    ArtifactSummary,
    ArtifactVersion,
    Citation,
    Comment,
    CommentOrigin,
    CommentStatus,
    EdgeState,
    LineageEdge,
    StaleDependent,
    VersionPin,
)
from ember_code.core.artifacts.provenance import Passage
from ember_code.core.code_index.neo4j_schema import (
    ARTIFACT_SCHEMA_STATEMENTS,
    DEFAULT_DATABASE,
)

logger = logging.getLogger(__name__)

# How many candidates to pull from the vector index per requested
# hit. Superseded versions are ranked alongside heads and filtered
# afterwards, so the factor has to cover an artifact's revision
# depth. 4 matches the code index's over-fetch for the same reason.
DEFAULT_OVERFETCH: int = 4


class WriteOutcome:
    """What :meth:`Neo4jArtifactClient.append_version` actually wrote.

    Carries ``unresolved_pins`` rather than raising on them: a write
    that cites one stale id should still store the document and report
    the bad reference, not lose the content.
    """

    def __init__(
        self,
        *,
        artifact_id: str,
        version_id: str,
        n: int,
        edges_written: int,
        unresolved_pins: list[str],
    ) -> None:
        self.artifact_id = artifact_id
        self.version_id = version_id
        self.n = n
        self.edges_written = edges_written
        self.unresolved_pins = unresolved_pins


class Neo4jArtifactClient:
    """Per-project artifact store on Neo4j.

    Construction is cheap; every method opens a fresh session.
    """

    def __init__(
        self,
        driver: AsyncDriver,
        project_id: str,
        *,
        overfetch: int = DEFAULT_OVERFETCH,
        codec: ArtifactMetadataCodec | None = None,
    ) -> None:
        self._driver = driver
        self._project_id = project_id
        self._db_name = DEFAULT_DATABASE
        self._overfetch = max(1, overfetch)
        self._codec = codec or ArtifactMetadataCodec()

    @property
    def project_id(self) -> str:
        return self._project_id

    def session(self) -> AsyncSession:
        return self._driver.session(database=self._db_name)

    async def apply_schema(self) -> None:
        """Apply the artifact DDL; re-applies cleanly.

        Every statement is ``IF NOT EXISTS``, so attach is idempotent.
        """
        async with self.session() as session:
            for stmt in ARTIFACT_SCHEMA_STATEMENTS:
                await session.run(stmt)

    async def close(self) -> None:
        """No-op — the driver is shared and owned by the runtime."""

    # ── Writes ────────────────────────────────────────────────────────

    async def append_version(
        self,
        *,
        artifact_id: str,
        name: str,
        content: str,
        kind: str | None = None,
        embedding: list[float] | None = None,
        chunks: list[tuple[str, int, int]] | None = None,
        chunk_embeddings: list[list[float]] | None = None,
        metadata: dict[str, str] | None = None,
        author: str = "",
        session_id: str = "",
        origin: str = "",
        pins: list[VersionPin] | None = None,
        citations: list[Citation] | None = None,
    ) -> WriteOutcome:
        """Append revision ``n+1`` and move the head pointer.

        Never overwrites: the previous version keeps its content and
        loses only its ``head`` flag. ``pins`` with ``n == 0`` resolve
        to the source's head *at write time*, so an agent citing a
        source by name gets an honest pin without knowing version
        numbers.
        """
        content_hash = self._codec.content_hash(content)
        meta_json = json.dumps(dict(metadata or {}))
        chunk_rows = list(chunks or [])
        # Positional, so a short list leaves the tail unembedded
        # rather than shifting every vector onto the wrong passage.
        vectors = list(chunk_embeddings or [])
        pin_rows = [
            {"artifact_id": p.artifact_id, "n": p.n, "state": p.state.value} for p in (pins or [])
        ]
        citation_rows = [
            {
                "source_id": c.source_id,
                "url": c.url,
                "kind": c.kind,
                "title": c.title,
                "fetched_at": c.fetched_at,
            }
            for c in (citations or [])
        ]
        async with self.session() as session:
            return await session.execute_write(
                self._append_version_tx,
                artifact_id,
                self._project_id,
                name,
                kind,
                content,
                content_hash,
                meta_json,
                author,
                session_id,
                origin,
                embedding,
                chunk_rows,
                pin_rows,
                citation_rows,
                vectors,
            )

    @staticmethod
    async def _append_version_tx(
        tx: Any,
        artifact_id: str,
        project_id: str,
        name: str,
        kind: str | None,
        content: str,
        content_hash: str,
        meta_json: str,
        author: str,
        session_id: str,
        origin: str,
        embedding: list[float] | None,
        chunk_rows: list[tuple[str, int, int]],
        pin_rows: list[dict[str, Any]],
        citation_rows: list[dict[str, Any]],
        vectors: list[list[float]],
    ) -> WriteOutcome:
        # Statement 1 — artifact upsert, head demotion, version create.
        # ``head_n`` is read and written inside one statement so two
        # concurrent writers can't both claim the same n; the write
        # lock the MERGE takes on the :Artifact node serialises them.
        result = await tx.run(
            "MERGE (a:Artifact {artifact_id: $aid, project_hash: $proj}) "
            "ON CREATE SET a.created_at = datetime(), a.head_n = 0, "
            "a.author = $author, a.origin = $origin "
            "SET a.name = $name, a.updated_at = datetime(), "
            "a.kind = coalesce($kind, a.kind, 'note') "
            "WITH a, coalesce(a.head_n, 0) + 1 AS n "
            # Classic ``CALL { WITH ... }`` rather than the 5.23
            # variable-scope spelling: the runtime is pinned to 5.26
            # today, but this store is also driven by test doubles and
            # older bundles, and the older form parses on both.
            "CALL { "
            "  WITH a "
            "  OPTIONAL MATCH (a)-[:HAS_VERSION]->(old:Version {head: true}) "
            "  SET old.head = false "
            "} "
            "SET a.head_n = n "
            "CREATE (a)-[:HAS_VERSION]->(v:Version {"
            "  version_id: $aid + '::' + toString(n), artifact_id: $aid, "
            "  n: n, content: $content, content_hash: $chash, head: true, "
            "  project_hash: $proj, created_at: datetime(), author: $author, "
            "  session_id: $sid, meta_json: $meta_json, embedding: $embedding"
            "}) "
            "RETURN n AS n, v.version_id AS version_id",
            aid=artifact_id,
            proj=project_id,
            name=name,
            kind=kind,
            content=content,
            chash=content_hash,
            meta_json=meta_json,
            author=author,
            sid=session_id,
            origin=origin,
            embedding=embedding,
        )
        record = await result.single()
        if record is None:  # pragma: no cover — MERGE always returns
            raise RuntimeError(f"append_version wrote nothing for {artifact_id}")
        n = int(record["n"])
        version_id = str(record["version_id"])

        # Statement 2 — chunks. Separate because ``UNWIND`` over an
        # empty list drops the row, which would take the edges with it.
        if chunk_rows:
            await tx.run(
                "MATCH (v:Version {version_id: $vid}) "
                "UNWIND $rows AS row "
                "CREATE (v)-[:HAS_CHUNK]->(:Chunk {"
                "  chunk_id: $vid + '::' + toString(row.i), parent_id: $vid, "
                "  text: row.text, line_from: row.line_from, "
                "  line_to: row.line_to, project_hash: $proj, "
                "  ordinal: row.i, embedding: row.embedding"
                "})",
                vid=version_id,
                proj=project_id,
                rows=[
                    {
                        "i": i,
                        "text": text,
                        "line_from": lf,
                        "line_to": lt,
                        # ``None`` where no vector was supplied. A
                        # chunk without one is simply not a candidate
                        # for passage matching — it is not an error,
                        # and it is what every chunk written before
                        # this field existed looks like.
                        "embedding": vectors[i] if i < len(vectors) else None,
                    }
                    for i, (text, lf, lt) in enumerate(chunk_rows)
                ],
            )

        # Statement 3 — derivation edges. ``pin.n = 0`` means "head at
        # write time"; resolving it here rather than in Python keeps
        # the read and the pin in the same transaction, so a source
        # revised concurrently can't produce a pin to a version that
        # was never the head.
        edges_written = 0
        resolved: set[str] = set()
        if pin_rows:
            edge_result = await tx.run(
                "MATCH (v:Version {version_id: $vid}) "
                "UNWIND $pins AS pin "
                "MATCH (src:Artifact {artifact_id: pin.artifact_id, project_hash: $proj}) "
                "WITH v, pin, src, "
                "  CASE WHEN pin.n <= 0 THEN src.head_n ELSE pin.n END AS pn "
                "MATCH (src)-[:HAS_VERSION]->(sv:Version {n: pn}) "
                "MERGE (v)-[d:DERIVED_FROM]->(sv) "
                "SET d.pinned_n = pn, d.state = pin.state, d.at = $now, "
                "    d.session_id = $sid "
                "RETURN pin.artifact_id AS artifact_id",
                vid=version_id,
                proj=project_id,
                pins=pin_rows,
                now=ArtifactMetadataCodec().now_iso(),
                sid=session_id,
            )
            for row in (await edge_result.to_eager_result()).records:
                edges_written += 1
                resolved.add(str(row["artifact_id"]))

        # Statement 4 — external citations.
        if citation_rows:
            await tx.run(
                "MATCH (v:Version {version_id: $vid}) "
                "UNWIND $rows AS row "
                "MERGE (s:Source {source_id: row.source_id, project_hash: $proj}) "
                "SET s.url = row.url, s.kind = row.kind, s.title = row.title, "
                "    s.fetched_at = row.fetched_at "
                "MERGE (v)-[:CITES]->(s)",
                vid=version_id,
                proj=project_id,
                rows=citation_rows,
            )

        unresolved = [p["artifact_id"] for p in pin_rows if p["artifact_id"] not in resolved]
        return WriteOutcome(
            artifact_id=artifact_id,
            version_id=version_id,
            n=n,
            edges_written=edges_written,
            unresolved_pins=unresolved,
        )

    async def record_candidates(
        self,
        *,
        version_id: str,
        pins: list[VersionPin],
    ) -> int:
        """Attach ``candidate`` edges for what the agent read.

        Never downgrades: an edge already ``confirmed`` (or explicitly
        ``dismissed``) keeps its state, so re-reading a source the user
        already ruled out does not resurrect it.
        """
        if not pins:
            return 0
        rows = [{"artifact_id": p.artifact_id, "n": p.n} for p in pins]
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {version_id: $vid, project_hash: $proj}) "
                "UNWIND $rows AS row "
                "MATCH (src:Artifact {artifact_id: row.artifact_id, project_hash: $proj}) "
                "WITH v, row, src, "
                "  CASE WHEN row.n <= 0 THEN src.head_n ELSE row.n END AS pn "
                "MATCH (src)-[:HAS_VERSION]->(sv:Version {n: pn}) "
                "WHERE sv <> v "
                "MERGE (v)-[d:DERIVED_FROM]->(sv) "
                "ON CREATE SET d.pinned_n = pn, d.state = $candidate, d.at = $now "
                "RETURN count(d) AS n",
                vid=version_id,
                proj=self._project_id,
                rows=rows,
                candidate=EdgeState.CANDIDATE.value,
                now=self._codec.now_iso(),
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def project_comments(self, comments: list[Comment]) -> int:
        """Mirror server-side comments into the local graph.

        Comments are captured where the reader is — on the server —
        and pulled down on sync. Writing them into the graph rather
        than holding them in a cache is what makes the history
        *local*: once projected, "which documents drew comments after
        their source moved" is one query alongside staleness and
        lineage, and it still answers with the network down.

        Idempotent by ``comment_id``, because sync pulls on
        ``updated_at`` and a comment that changed status arrives again
        with the same id. The ``SET`` is unconditional for the same
        reason: the server is the authority on a comment's status, so
        a local copy that refused to move would quietly show a
        resolved thread as open forever.

        A comment whose version is not here yet is **skipped, not
        dropped** — ``MATCH`` simply fails to bind. That happens
        whenever a collaborator comments on a version this machine has
        not pulled, and the next sync after the chain catches up
        projects it, because the cursor is the server's and does not
        advance past rows this side failed to place.
        """
        if not comments:
            return 0
        rows = [
            {
                "comment_id": c.comment_id,
                "artifact_id": c.artifact_id,
                "n": c.n,
                "thread_id": c.thread_id,
                "parent_id": c.parent_id,
                "quote": c.quote,
                "quote_start": c.quote_start,
                "quote_end": c.quote_end,
                "body": c.body,
                "suggested_text": c.suggested_text,
                "status": c.status.value,
                "resolved_in_n": c.resolved_in_n,
                "author": c.author,
                "at": c.at,
                "origin": c.origin.value,
            }
            for c in comments
        ]
        async with self.session() as session:
            result = await session.run(
                "UNWIND $rows AS row "
                "MATCH (a:Artifact {artifact_id: row.artifact_id, project_hash: $proj}) "
                "MATCH (a)-[:HAS_VERSION]->(v:Version {n: row.n}) "
                "MERGE (c:Comment {comment_id: row.comment_id, project_hash: $proj}) "
                "SET c.artifact_id = row.artifact_id, c.n = row.n, "
                "    c.thread_id = row.thread_id, c.parent_id = row.parent_id, "
                "    c.quote = row.quote, c.quote_start = row.quote_start, "
                "    c.quote_end = row.quote_end, c.body = row.body, "
                "    c.suggested_text = row.suggested_text, c.status = row.status, "
                "    c.resolved_in_n = row.resolved_in_n, c.author = row.author, "
                "    c.at = row.at, c.origin = row.origin "
                "MERGE (c)-[:ON]->(v) "
                "RETURN count(c) AS n",
                proj=self._project_id,
                rows=rows,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def comments(
        self,
        *,
        artifact_id: str | None = None,
        n: int | None = None,
        open_only: bool = False,
        limit: int = 500,
    ) -> list[Comment]:
        """Read projected comments, newest last.

        Every filter is optional so one method serves both the
        document view ("comments on this version") and the review
        queue ("everything still open, anywhere").
        """
        clauses = ["c.project_hash = $proj"]
        params: dict[str, Any] = {"proj": self._project_id, "limit": limit}
        if artifact_id is not None:
            clauses.append("c.artifact_id = $aid")
            params["aid"] = artifact_id
        if n is not None:
            clauses.append("c.n = $n")
            params["n"] = n
        if open_only:
            clauses.append("c.status = $open")
            params["open"] = CommentStatus.OPEN.value
        async with self.session() as session:
            result = await session.run(
                "MATCH (c:Comment) WHERE "
                + " AND ".join(clauses)
                + " RETURN c ORDER BY c.at LIMIT $limit",
                **params,
            )
            return [self._to_comment(record["c"]) async for record in result]

    async def passages(self, *, artifact_id: str, n: int) -> list[Passage]:
        """Every chunk of one version, in document order, with vectors.

        Ordered by ``ordinal`` — the chunker's own output order, which
        is the authority on what comes first. Ordering by ``line_from``
        instead made document order depend on line numbers being
        right, and any chunk whose span could not be resolved sorted
        to the top of the document. Chunk ids are no good either: they
        end in a decimal index, so ``::10`` sorts before ``::2``.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Artifact {artifact_id: $aid, project_hash: $proj}) "
                "MATCH (a)-[:HAS_VERSION]->(v:Version {n: $n}) "
                "MATCH (v)-[:HAS_CHUNK]->(c:Chunk) "
                "RETURN c ORDER BY coalesce(c.ordinal, c.line_from), c.line_from",
                aid=artifact_id,
                proj=self._project_id,
                n=n,
            )
            return [
                Passage(
                    chunk_id=str(record["c"].get("chunk_id") or ""),
                    text=str(record["c"].get("text") or ""),
                    line_from=int(record["c"].get("line_from") or 0),
                    line_to=int(record["c"].get("line_to") or 0),
                    embedding=list(record["c"].get("embedding") or []) or None,
                )
                async for record in result
            ]

    async def versions_needing_rechunk(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Versions whose chunks predate ordinals or vectors.

        ``ordinal`` is the marker: a chunk written before line spans
        were resolved whitespace-tolerantly has none, and its stored
        span is not to be trusted. Recomputing is cheap next to
        carrying a citation that points at line zero.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {project_hash: $proj})-[:HAS_CHUNK]->(c:Chunk) "
                "WITH v, count(c) AS total, "
                "     count(c.ordinal) AS ordered, count(c.embedding) AS vectored "
                "WHERE ordered < total OR vectored < total "
                "MATCH (a:Artifact)-[:HAS_VERSION]->(v) "
                "RETURN v.version_id AS version_id, v.content AS content, "
                "       a.artifact_id AS artifact_id, v.n AS n "
                "ORDER BY v.version_id LIMIT $limit",
                proj=self._project_id,
                limit=limit,
            )
            return [dict(record) async for record in result]

    async def replace_chunks(
        self,
        *,
        version_id: str,
        rows: list[tuple[str, int, int]],
        vectors: list[list[float]],
    ) -> int:
        """Swap a version's chunks for a freshly computed set.

        Delete-then-create in one transaction. A version briefly
        holding half its old chunks and half its new ones would answer
        a provenance query with passages from two different splits.
        """
        payload = [
            {
                "i": i,
                "text": text,
                "line_from": lf,
                "line_to": lt,
                "embedding": vectors[i] if i < len(vectors) else None,
            }
            for i, (text, lf, lt) in enumerate(rows)
        ]

        async def _tx(tx: Any) -> int:
            await tx.run(
                "MATCH (v:Version {version_id: $vid})-[:HAS_CHUNK]->(c:Chunk) DETACH DELETE c",
                vid=version_id,
            )
            if not payload:
                return 0
            result = await tx.run(
                "MATCH (v:Version {version_id: $vid}) "
                "UNWIND $rows AS row "
                "CREATE (v)-[:HAS_CHUNK]->(:Chunk {"
                "  chunk_id: $vid + '::' + toString(row.i), parent_id: $vid, "
                "  text: row.text, line_from: row.line_from, "
                "  line_to: row.line_to, project_hash: $proj, "
                "  ordinal: row.i, embedding: row.embedding"
                "}) RETURN count(*) AS n",
                vid=version_id,
                proj=self._project_id,
                rows=payload,
            )
            record = await result.single()
            return int(record["n"]) if record else 0

        async with self.session() as session:
            return await session.execute_write(_tx)

    async def chunks_without_embeddings(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Chunks written before vectors were stored, oldest first.

        Every document in a store that predates this field has them,
        and until they are filled that document cannot be placed
        against its source at all.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (c:Chunk {project_hash: $proj}) WHERE c.embedding IS NULL "
                "RETURN c.chunk_id AS chunk_id, c.text AS text "
                "ORDER BY c.chunk_id LIMIT $limit",
                proj=self._project_id,
                limit=limit,
            )
            return [
                {"chunk_id": str(r["chunk_id"]), "text": str(r["text"] or "")} async for r in result
            ]

    async def set_chunk_embeddings(self, rows: list[dict[str, Any]]) -> int:
        """Fill in vectors for chunks that had none."""
        if not rows:
            return 0
        async with self.session() as session:
            result = await session.run(
                "UNWIND $rows AS row "
                "MATCH (c:Chunk {chunk_id: row.chunk_id, project_hash: $proj}) "
                "SET c.embedding = row.embedding "
                "RETURN count(c) AS n",
                proj=self._project_id,
                rows=rows,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def open_comment_count(self) -> int:
        async with self.session() as session:
            result = await session.run(
                "MATCH (c:Comment {project_hash: $proj, status: $open}) RETURN count(c) AS n",
                proj=self._project_id,
                open=CommentStatus.OPEN.value,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    @staticmethod
    def _to_comment(node: Any) -> Comment:
        raw = str(node.get("status") or CommentStatus.OPEN.value)
        try:
            status = CommentStatus(raw)
        except ValueError:
            # A status this build does not know about must not widen
            # anything. Treating it as open puts it in front of a
            # person instead of hiding it.
            logger.warning("unknown comment status %r; treating as open", raw)
            status = CommentStatus.OPEN
        raw_origin = str(node.get("origin") or CommentOrigin.WEB.value)
        try:
            origin = CommentOrigin(raw_origin)
        except ValueError:
            # Unknown origin falls to ``WEB``, the narrower reading: a
            # comment the agent treats as history at worst goes unacted
            # on, where one wrongly treated as an instruction gets
            # carried out by itself.
            logger.warning("unknown comment origin %r; treating as web", raw_origin)
            origin = CommentOrigin.WEB
        return Comment(
            comment_id=str(node.get("comment_id") or ""),
            artifact_id=str(node.get("artifact_id") or ""),
            n=int(node.get("n") or 0),
            thread_id=str(node.get("thread_id") or ""),
            parent_id=node.get("parent_id"),
            quote=str(node.get("quote") or ""),
            quote_start=node.get("quote_start"),
            quote_end=node.get("quote_end"),
            body=str(node.get("body") or ""),
            suggested_text=node.get("suggested_text"),
            status=status,
            resolved_in_n=node.get("resolved_in_n"),
            author=str(node.get("author") or ""),
            at=str(node.get("at") or ""),
            origin=origin,
        )

    async def set_edge_state(
        self,
        *,
        artifact_id: str,
        n: int,
        source_artifact_id: str,
        state: EdgeState,
    ) -> bool:
        """Promote a candidate to confirmed, or dismiss it."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {version_id: $vid, project_hash: $proj})"
                "-[d:DERIVED_FROM]->(sv:Version {artifact_id: $src}) "
                "SET d.state = $state "
                "RETURN count(d) AS n",
                vid=self._codec.version_id(artifact_id, n),
                proj=self._project_id,
                src=source_artifact_id,
                state=state.value,
            )
            record = await result.single()
        return bool(record and record["n"])

    async def edge_review(
        self, *, artifact_id: str, n: int, source_artifact_id: str
    ) -> dict[str, Any] | None:
        """What was already worked out about this edge, if anything.

        Both halves of a source review are expensive and neither
        changes on its own: the overlap is an embedding comparison of
        every passage against every passage, and the verdict is a
        model call over both documents. Recomputing them each time a
        reviewer opens the row charges for the same answer repeatedly
        and — for the verdict — can quietly return a *different* one,
        so a reviewer who reopens a row to think about it finds the
        recommendation has changed under them.

        Returns the stored properties verbatim. Whether they are still
        about the right pair of versions is the caller's question, and
        it answers it from ``review_dependent_n`` / ``review_source_n``.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {version_id: $vid, project_hash: $proj})"
                "-[d:DERIVED_FROM]->(sv:Version {artifact_id: $src}) "
                "RETURN properties(d) AS props",
                vid=self._codec.version_id(artifact_id, n),
                proj=self._project_id,
                src=source_artifact_id,
            )
            record = await result.single()
        return dict(record["props"]) if record and record["props"] else None

    async def set_edge_review(
        self, *, artifact_id: str, n: int, source_artifact_id: str, review: dict[str, Any]
    ) -> bool:
        """Keep what was worked out, against the edge it is about.

        Written with ``+=`` so it adds to the edge rather than
        replacing it: ``state``, ``pinned_n`` and ``at`` are the
        edge's own facts and a review must never be able to overwrite
        one of them.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {version_id: $vid, project_hash: $proj})"
                "-[d:DERIVED_FROM]->(sv:Version {artifact_id: $src}) "
                "SET d += $review "
                "RETURN count(d) AS n",
                vid=self._codec.version_id(artifact_id, n),
                proj=self._project_id,
                src=source_artifact_id,
                review=dict(review),
            )
            record = await result.single()
        return bool(record and record["n"])

    async def repin(
        self,
        *,
        artifact_id: str,
        n: int,
        source_artifact_id: str,
    ) -> int:
        """Move a dependent's edge onto the source's current head.

        Used by the "accept, content unchanged" path — the reviewer
        read the source diff and decided the dependent is still
        correct. Re-pointing the edge is what stops that decision from
        being re-litigated on every staleness sweep.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {version_id: $vid, project_hash: $proj})"
                "-[d:DERIVED_FROM]->(old:Version {artifact_id: $src}) "
                "MATCH (sa:Artifact {artifact_id: $src, project_hash: $proj})"
                "-[:HAS_VERSION]->(new:Version {head: true}) "
                "WHERE new <> old "
                "DELETE d "
                "MERGE (v)-[nd:DERIVED_FROM]->(new) "
                "SET nd.pinned_n = new.n, nd.state = $confirmed, nd.at = $now "
                "RETURN count(nd) AS n",
                vid=self._codec.version_id(artifact_id, n),
                proj=self._project_id,
                src=source_artifact_id,
                confirmed=EdgeState.CONFIRMED.value,
                now=self._codec.now_iso(),
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def prune_candidates(self, *, before_iso: str) -> int:
        """Drop unconfirmed edges older than ``before_iso``.

        Capture is deliberately generous, so without pruning the
        candidate set grows without bound and the review surface
        becomes unusable — which is the same thing as having no
        review surface.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {project_hash: $proj})-[d:DERIVED_FROM]->() "
                "WHERE d.state = $candidate AND d.at < $before "
                "DELETE d "
                "RETURN count(d) AS n",
                proj=self._project_id,
                candidate=EdgeState.CANDIDATE.value,
                before=before_iso,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    # ── Reads ─────────────────────────────────────────────────────────

    async def search(
        self,
        *,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[ArtifactSearchHit]:
        """Vector search over **head versions only**.

        Superseded versions stay addressable by ``(artifact_id, n)``
        but never surface here: retrieving text the author has already
        replaced is how an agent confidently repeats a fact that was
        corrected three revisions ago.
        """
        async with self.session() as session:
            result = await session.run(
                "CALL db.index.vector.queryNodes('version_embedding', $k, $vec) "
                "YIELD node, score "
                "WHERE node.head AND node.project_hash = $proj "
                "MATCH (a:Artifact)-[:HAS_VERSION]->(node) "
                "RETURN a.artifact_id AS artifact_id, a.name AS name, "
                "a.kind AS kind, node.n AS n, node.content AS content, "
                "node.meta_json AS meta_json, score "
                "ORDER BY score DESC LIMIT $limit",
                k=limit * self._overfetch,
                vec=query_embedding,
                proj=self._project_id,
                limit=limit,
            )
            records = (await result.to_eager_result()).records
        return [
            ArtifactSearchHit(
                artifact_id=r["artifact_id"],
                name=r["name"] or "",
                kind=r["kind"] or "",
                n=int(r["n"] or 0),
                content=r["content"] or "",
                score=r["score"],
                project=self._project_id,
                metadata=_loads(r["meta_json"]),
            )
            for r in records
        ]

    async def count(self) -> int:
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Artifact {project_hash: $proj}) RETURN count(a) AS n",
                proj=self._project_id,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def version_count(self) -> int:
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {project_hash: $proj}) RETURN count(v) AS n",
                proj=self._project_id,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def candidate_count(self) -> int:
        async with self.session() as session:
            result = await session.run(
                "MATCH (v:Version {project_hash: $proj})-[d:DERIVED_FROM]->() "
                "WHERE d.state = $candidate AND v.head "
                "RETURN count(d) AS n",
                proj=self._project_id,
                candidate=EdgeState.CANDIDATE.value,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def set_parent(self, *, node_id: str, parent_id: str | None) -> bool:
        """Put a document or folder under another, or at the root.

        The parent is a **property, not a relationship**. A
        ``CHILD_OF`` edge would need a label to match on, and the two
        node kinds here do not share one — giving them one would make
        folders satisfy ``:Artifact``, which is what search, lineage,
        drift and the vector index all key on. A property costs one
        write and the tree is assembled from rows that are fetched
        whole anyway.

        Cycles are refused by the caller, against the same rows the
        tree is built from, so the answer is known before the write is
        attempted — see :func:`~ember_code.core.artifacts.tree.would_cycle`.
        ``None`` clears the parent and makes it a root.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (n) WHERE (n:Artifact OR n:Folder) "
                "AND coalesce(n.artifact_id, n.folder_id) = $nid "
                "AND n.project_hash = $proj "
                "SET n.parent_id = coalesce($pid, ''), n.updated_at = datetime() "
                "RETURN count(n) AS n",
                nid=node_id,
                pid=parent_id or None,
                proj=self._project_id,
            )
            record = await result.single()
        return bool(record and record["n"])

    async def set_archived(self, *, artifact_id: str, archived: bool) -> int | None:
        """Put a document away, or bring it back.

        A flag, not a delete. Every version and every ``DERIVED_FROM``
        edge stays exactly as it was, so a document built on this one
        still resolves what it was built on and still reports drift
        when this one moves. What changes is only whether it is
        offered: out of the tree, out of the list, out of the way.

        **Anything under it moves up a level**, the way it does when a
        folder is removed. Archiving is a decision about one document,
        and leaving its children pointing at a parent that is no
        longer in the tree strands them under "a document you cannot
        open" — which is what the first version did, and it reads as
        the archive having swallowed them.

        Returns how many were moved, or ``None`` if there is no such
        document. Zero and "not found" are different answers and the
        caller has to be able to tell them apart.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Artifact {artifact_id: $aid, project_hash: $proj}) "
                "SET a.archived = $archived, a.updated_at = datetime() "
                "RETURN coalesce(a.parent_id, '') AS parent_id",
                aid=artifact_id,
                proj=self._project_id,
                archived=bool(archived),
            )
            record = await result.single()
            if record is None:
                return None
            if not archived:
                return 0
            moved = await session.run(
                "MATCH (c) WHERE (c:Artifact OR c:Folder) "
                "AND c.parent_id = $aid AND c.project_hash = $proj "
                "SET c.parent_id = $up, c.updated_at = datetime() "
                "RETURN count(c) AS n",
                aid=artifact_id,
                proj=self._project_id,
                up=record["parent_id"] or "",
            )
            moved_record = await moved.single()
        return int(moved_record["n"]) if moved_record else 0

    async def lineage_ties(self, artifact_id: str) -> list[dict[str, Any]]:
        """Every document this one is joined to by a derivation edge.

        **Both directions.** A document built on this one breaks when
        this one goes, which is the obvious half. The other half is
        this document's own sources: the edge is the record of what it
        was written from, and deleting the document destroys that
        record for the source as well — its dependents list is how
        anybody finds out the thing existed.

        Any version, not the head. Deleting an artifact takes its
        whole version chain with it, so a tie pinned to v2 is cut just
        as surely as one pinned to the head, and it is the older pins
        that are easiest to forget.

        ``direction`` is ``dependent`` when the other document was
        built on this one and ``source`` when this one was built on
        it. ``state`` comes back too: candidate and confirmed are not
        the same claim, and dismissed is not a claim at all.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (mine:Artifact {artifact_id: $aid, project_hash: $proj}) "
                "CALL { "
                "  WITH mine "
                "  MATCH (dv:Version)-[d:DERIVED_FROM]->(sv:Version) "
                "  MATCH (mine)-[:HAS_VERSION]->(sv) "
                "  MATCH (other:Artifact)-[:HAS_VERSION]->(dv) "
                "  RETURN other, d, 'dependent' AS direction "
                "  UNION "
                "  WITH mine "
                "  MATCH (mv:Version)-[d:DERIVED_FROM]->(ov:Version) "
                "  MATCH (mine)-[:HAS_VERSION]->(mv) "
                "  MATCH (other:Artifact)-[:HAS_VERSION]->(ov) "
                "  RETURN other, d, 'source' AS direction "
                "} "
                # A document is never its own tie. Self-edges exist in
                # stores written before that was refused at the write,
                # and one here would make every such document
                # undeletable on its own account.
                "WITH other, d, direction WHERE other.artifact_id <> $aid "
                "RETURN DISTINCT other.artifact_id AS artifact_id, "
                "coalesce(other.name, '') AS name, coalesce(d.state, '') AS state, "
                "coalesce(other.archived, false) AS archived, direction "
                "ORDER BY name",
                aid=artifact_id,
                proj=self._project_id,
            )
            records = (await result.to_eager_result()).records
        return [
            {
                "artifact_id": r["artifact_id"],
                "name": r["name"],
                "state": r["state"],
                "archived": bool(r["archived"]),
                "direction": r["direction"],
            }
            for r in records
        ]

    async def create_folder(self, *, name: str, parent_id: str = "", folder_id: str = "") -> str:
        """A place to put things, holding nothing itself.

        A separate label from ``:Artifact`` deliberately. A folder with
        that label would turn up in search, in lineage, in drift and in
        the vector index as a document with no text — every one of
        those keys on it.

        ``folder_id`` is given when the server has already named it.
        The graph mirrors the shared copy, and two copies that mint
        their own ids agree about a folder's existence and about
        nothing else — every later operation would address the wrong
        one. Minted here only when nothing else has.
        """
        folder_id = folder_id or uuid.uuid4().hex[:16]
        async with self.session() as session:
            await session.run(
                "CREATE (f:Folder {folder_id: $fid, project_hash: $proj, "
                "name: $name, parent_id: $pid, "
                "created_at: datetime(), updated_at: datetime()})",
                fid=folder_id,
                proj=self._project_id,
                name=name,
                pid=parent_id or "",
            )
        return folder_id

    async def rename_folder(self, *, folder_id: str, name: str) -> bool:
        async with self.session() as session:
            result = await session.run(
                "MATCH (f:Folder {folder_id: $fid, project_hash: $proj}) "
                "SET f.name = $name, f.updated_at = datetime() RETURN count(f) AS n",
                fid=folder_id,
                proj=self._project_id,
                name=name,
            )
            record = await result.single()
        return bool(record and record["n"])

    async def delete_folder(self, *, folder_id: str) -> int:
        """Remove a folder, promoting whatever was inside it.

        Never deletes documents. A folder is an arrangement, and
        deleting an arrangement must not delete the things arranged —
        so its children move up to its own parent rather than being
        orphaned or removed. Returns how many moved.
        """
        async with self.session() as session:
            found = await session.run(
                "MATCH (f:Folder {folder_id: $fid, project_hash: $proj}) "
                "RETURN coalesce(f.parent_id, '') AS parent",
                fid=folder_id,
                proj=self._project_id,
            )
            record = await found.single()
            if record is None:
                return 0
            promoted = await session.run(
                "MATCH (n) WHERE (n:Artifact OR n:Folder) "
                "AND n.project_hash = $proj AND n.parent_id = $fid "
                "SET n.parent_id = $parent RETURN count(n) AS n",
                fid=folder_id,
                parent=record["parent"] or "",
                proj=self._project_id,
            )
            counted = await promoted.single()
            moved = counted["n"] if counted is not None else 0
            await session.run(
                "MATCH (f:Folder {folder_id: $fid, project_hash: $proj}) DELETE f",
                fid=folder_id,
                proj=self._project_id,
            )
        return int(moved or 0)

    async def folders(self) -> list[dict[str, Any]]:
        """Every folder in this project."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (f:Folder {project_hash: $proj}) "
                "RETURN f.folder_id AS node_id, coalesce(f.name, '') AS name, "
                "coalesce(f.parent_id, '') AS parent_id, "
                "toString(f.updated_at) AS updated_at "
                "ORDER BY f.name",
                proj=self._project_id,
            )
            records = (await result.to_eager_result()).records
        return [
            {
                "node_id": r["node_id"],
                "name": r["name"],
                "parent_id": r["parent_id"],
                "updated_at": r["updated_at"] or "",
                "is_folder": True,
            }
            for r in records
        ]

    async def list_artifacts(
        self, *, limit: int = 1000, include_archived: bool = False
    ) -> list[ArtifactSummary]:
        """Every artifact in this project, heads only.

        Archived ones are left out by default. Archiving is only
        worth anything if the archived thing stops turning up, and
        every caller that lists documents is somewhere it should not
        turn up. The two places that pass ``True`` are the tree, when
        the reader has asked to see them, and restoring.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Artifact {project_hash: $proj}) "
                + ("" if include_archived else "WHERE NOT coalesce(a.archived, false) ")
                + "OPTIONAL MATCH (a)-[:HAS_VERSION]->(v:Version) "
                "WITH a, count(v) AS versions "
                "RETURN a.artifact_id AS artifact_id, a.name AS name, "
                "a.kind AS kind, coalesce(a.head_n, 0) AS head_n, "
                "toString(a.created_at) AS created_at, "
                "toString(a.updated_at) AS updated_at, "
                "coalesce(a.author, '') AS author, "
                "coalesce(a.origin, '') AS origin, "
                "coalesce(a.parent_id, '') AS parent_id, "
                "coalesce(a.archived, false) AS archived, versions "
                "ORDER BY a.updated_at DESC LIMIT $limit",
                proj=self._project_id,
                limit=limit,
            )
            records = (await result.to_eager_result()).records
        return [_summary(r) for r in records]

    async def get_artifact(self, artifact_id: str) -> ArtifactSummary | None:
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Artifact {artifact_id: $aid, project_hash: $proj}) "
                "OPTIONAL MATCH (a)-[:HAS_VERSION]->(v:Version) "
                "WITH a, count(v) AS versions "
                "RETURN a.artifact_id AS artifact_id, a.name AS name, "
                "a.kind AS kind, coalesce(a.head_n, 0) AS head_n, "
                "toString(a.created_at) AS created_at, "
                "toString(a.updated_at) AS updated_at, "
                "coalesce(a.author, '') AS author, "
                "coalesce(a.origin, '') AS origin, "
                "coalesce(a.archived, false) AS archived, versions",
                aid=artifact_id,
                proj=self._project_id,
            )
            record = await result.single()
        return _summary(record) if record else None

    async def find_by_name(self, name: str) -> ArtifactSummary | None:
        """Resolve a human-typed name to an artifact.

        Exact match on the normalised id first — the agent usually
        cites the name it was shown — then a case-insensitive scan, so
        "auth design" finds "Auth Design" without a full-text query
        the caller has to escape.
        """
        by_id = await self.get_artifact(self._codec.artifact_id(name))
        if by_id is not None:
            return by_id
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Artifact {project_hash: $proj}) "
                "WHERE toLower(a.name) = toLower($name) "
                "OPTIONAL MATCH (a)-[:HAS_VERSION]->(v:Version) "
                "WITH a, count(v) AS versions "
                "RETURN a.artifact_id AS artifact_id, a.name AS name, "
                "a.kind AS kind, coalesce(a.head_n, 0) AS head_n, "
                "toString(a.created_at) AS created_at, "
                "toString(a.updated_at) AS updated_at, "
                "coalesce(a.author, '') AS author, "
                "coalesce(a.origin, '') AS origin, "
                "coalesce(a.archived, false) AS archived, versions "
                "LIMIT 1",
                proj=self._project_id,
                name=name,
            )
            record = await result.single()
        return _summary(record) if record else None

    async def versions(self, artifact_id: str, *, limit: int = 200) -> list[ArtifactVersion]:
        """The revision chain, newest first — content included."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (:Artifact {artifact_id: $aid, project_hash: $proj})"
                "-[:HAS_VERSION]->(v:Version) "
                "RETURN v.artifact_id AS artifact_id, v.version_id AS version_id, "
                "v.n AS n, v.content AS content, v.content_hash AS content_hash, "
                "coalesce(v.head, false) AS head, toString(v.created_at) AS created_at, "
                "coalesce(v.author, '') AS author, coalesce(v.session_id, '') AS session_id, "
                "v.meta_json AS meta_json "
                "ORDER BY v.n DESC LIMIT $limit",
                aid=artifact_id,
                proj=self._project_id,
                limit=limit,
            )
            records = (await result.to_eager_result()).records
        return [_version(r) for r in records]

    async def version(self, artifact_id: str, n: int) -> ArtifactVersion | None:
        """One exact revision. ``n <= 0`` resolves to head."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Artifact {artifact_id: $aid, project_hash: $proj})"
                "-[:HAS_VERSION]->(v:Version) "
                "WHERE (CASE WHEN $n <= 0 THEN v.head = true ELSE v.n = $n END) "
                "RETURN v.artifact_id AS artifact_id, v.version_id AS version_id, "
                "v.n AS n, v.content AS content, v.content_hash AS content_hash, "
                "coalesce(v.head, false) AS head, toString(v.created_at) AS created_at, "
                "coalesce(v.author, '') AS author, coalesce(v.session_id, '') AS session_id, "
                "v.meta_json AS meta_json "
                "LIMIT 1",
                aid=artifact_id,
                proj=self._project_id,
                n=n,
            )
            record = await result.single()
        return _version(record) if record else None

    async def lineage(self, artifact_id: str, n: int = 0) -> ArtifactLineage:
        """Both directions around one version, plus its citations."""
        target = await self.version(artifact_id, n)
        if target is None:
            return ArtifactLineage(artifact_id=artifact_id, n=n)
        async with self.session() as session:
            sources = await self._edges(
                session,
                "MATCH (v:Version {version_id: $vid})-[d:DERIVED_FROM]->(sv:Version) "
                "MATCH (sa:Artifact)-[:HAS_VERSION]->(sv) "
                "MATCH (va:Artifact)-[:HAS_VERSION]->(v) "
                # A document is never its own source. Refused at the
                # write now, but stores written before that carry the
                # edges, and a filter here fixes what is on screen
                # without deleting anybody's data.
                "WHERE sa.artifact_id <> va.artifact_id "
                "RETURN v.artifact_id AS from_artifact_id, v.n AS from_n, "
                "va.name AS from_name, sv.artifact_id AS to_artifact_id, "
                "sv.n AS to_n, sa.name AS to_name, d.state AS state, "
                "coalesce(d.pinned_n, sv.n) AS pinned_n, "
                "coalesce(sa.head_n, 0) AS source_head_n, coalesce(d.at, '') AS at "
                "ORDER BY sa.name",
                vid=target.version_id,
            )
            dependents = await self._edges(
                session,
                "MATCH (dv:Version)-[d:DERIVED_FROM]->(v:Version {version_id: $vid}) "
                "MATCH (da:Artifact)-[:HAS_VERSION]->(dv) "
                "MATCH (va:Artifact)-[:HAS_VERSION]->(v) "
                "RETURN dv.artifact_id AS from_artifact_id, dv.n AS from_n, "
                "da.name AS from_name, v.artifact_id AS to_artifact_id, "
                "v.n AS to_n, va.name AS to_name, d.state AS state, "
                "coalesce(d.pinned_n, v.n) AS pinned_n, "
                "coalesce(va.head_n, 0) AS source_head_n, coalesce(d.at, '') AS at "
                "ORDER BY da.name",
                vid=target.version_id,
            )
            cite_result = await session.run(
                "MATCH (:Version {version_id: $vid})-[:CITES]->(s:Source) "
                "RETURN s.url AS url, coalesce(s.kind, '') AS kind, "
                "coalesce(s.title, '') AS title, coalesce(s.fetched_at, '') AS fetched_at "
                "ORDER BY s.url",
                vid=target.version_id,
            )
            citations = [
                Citation(
                    url=r["url"],
                    kind=r["kind"],
                    title=r["title"],
                    fetched_at=r["fetched_at"],
                )
                for r in (await cite_result.to_eager_result()).records
            ]
        return ArtifactLineage(
            artifact_id=artifact_id,
            n=target.n,
            sources=sources,
            dependents=dependents,
            citations=citations,
        )

    @staticmethod
    async def _edges(session: AsyncSession, cypher: str, **params: Any) -> list[LineageEdge]:
        result = await session.run(cypher, **params)
        records = (await result.to_eager_result()).records
        return [
            LineageEdge(
                from_artifact_id=r["from_artifact_id"],
                from_n=int(r["from_n"] or 0),
                from_name=r["from_name"] or "",
                to_artifact_id=r["to_artifact_id"],
                to_n=int(r["to_n"] or 0),
                to_name=r["to_name"] or "",
                state=_edge_state(r["state"]),
                pinned_n=int(r["pinned_n"] or 0),
                source_head_n=int(r["source_head_n"] or 0),
                at=r["at"] or "",
            )
            for r in records
        ]

    async def stale_dependents(self, *, limit: int = 100) -> list[StaleDependent]:
        """Confirmed derivations whose source has moved since pinning.

        Only ``head`` dependents are reported: a superseded version
        being out of date is a fact about history, not a thing anyone
        can fix.
        """
        async with self.session() as session:
            result = await session.run(
                "MATCH (dep:Version {project_hash: $proj})-[d:DERIVED_FROM]->(src:Version) "
                "MATCH (sa:Artifact)-[:HAS_VERSION]->(src) "
                "MATCH (da:Artifact)-[:HAS_VERSION]->(dep) "
                "WHERE d.state = $confirmed AND dep.head "
                "  AND coalesce(d.pinned_n, src.n) < coalesce(sa.head_n, 0) "
                # Same rule as lineage: a revision is not a dependent
                # of its own previous draft, and reporting it as one
                # put every document in the out-of-date queue for ever.
                "  AND sa.artifact_id <> da.artifact_id "
                "RETURN dep.artifact_id AS dependent_artifact_id, "
                "da.name AS dependent_name, dep.n AS dependent_n, "
                "sa.artifact_id AS source_artifact_id, sa.name AS source_name, "
                "coalesce(d.pinned_n, src.n) AS pinned_n, "
                "coalesce(sa.head_n, 0) AS source_head_n "
                "ORDER BY (coalesce(sa.head_n, 0) - coalesce(d.pinned_n, src.n)) DESC "
                "LIMIT $limit",
                proj=self._project_id,
                confirmed=EdgeState.CONFIRMED.value,
                limit=limit,
            )
            records = (await result.to_eager_result()).records
        return [
            StaleDependent(
                dependent_artifact_id=r["dependent_artifact_id"],
                dependent_name=r["dependent_name"] or "",
                dependent_n=int(r["dependent_n"] or 0),
                source_artifact_id=r["source_artifact_id"],
                source_name=r["source_name"] or "",
                pinned_n=int(r["pinned_n"] or 0),
                source_head_n=int(r["source_head_n"] or 0),
            )
            for r in records
        ]

    async def has_artifact(self, artifact_id: str) -> bool:
        async with self.session() as session:
            result = await session.run(
                "MATCH (a:Artifact {artifact_id: $aid, project_hash: $proj}) "
                "RETURN count(a) > 0 AS present",
                aid=artifact_id,
                proj=self._project_id,
            )
            record = await result.single()
        return bool(record["present"]) if record else False

    async def delete_artifact(self, artifact_id: str) -> bool:
        """Delete an artifact, every version, and their chunks.

        Dependents' edges go with it. They are not rewritten to point
        anywhere else — a dangling pin would claim a provenance that
        no longer exists, which is worse than a missing one.
        """
        async with self.session() as session:
            await session.run(
                "MATCH (a:Artifact {artifact_id: $aid, project_hash: $proj})"
                "-[:HAS_VERSION]->(v:Version) "
                "OPTIONAL MATCH (v)-[:HAS_CHUNK]->(c:Chunk) "
                "DETACH DELETE v, c",
                aid=artifact_id,
                proj=self._project_id,
            )
            await session.run(
                "MATCH (a:Artifact {artifact_id: $aid, project_hash: $proj}) DETACH DELETE a",
                aid=artifact_id,
                proj=self._project_id,
            )
        return True


# ── Row mapping ───────────────────────────────────────────────────────


def _loads(raw: Any) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def _edge_state(raw: Any) -> EdgeState:
    try:
        return EdgeState(str(raw))
    except ValueError:
        return EdgeState.CANDIDATE


def _maybe(record: Any, key: str) -> Any:
    """A field the record may not carry."""
    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def _summary(record: Any) -> ArtifactSummary:
    return ArtifactSummary(
        artifact_id=record["artifact_id"],
        name=record["name"] or "",
        kind=record["kind"] or "note",
        head_n=int(record["head_n"] or 0),
        created_at=record["created_at"] or "",
        updated_at=record["updated_at"] or "",
        author=record["author"] or "",
        origin=record["origin"] or "",
        version_count=int(record["versions"] or 0),
        # Tolerated rather than required: the same helper projects
        # records from queries written before the tree existed, and a
        # KeyError there would take out the artifact list.
        parent_id=(_maybe(record, "parent_id") or ""),
        archived=bool(_maybe(record, "archived") or False),
    )


def _version(record: Any) -> ArtifactVersion:
    return ArtifactVersion(
        artifact_id=record["artifact_id"],
        version_id=record["version_id"],
        n=int(record["n"] or 0),
        content=record["content"] or "",
        content_hash=record["content_hash"] or "",
        head=bool(record["head"]),
        created_at=record["created_at"] or "",
        author=record["author"] or "",
        session_id=record["session_id"] or "",
        metadata=_loads(record["meta_json"]),
    )
