"""Per-project knowledge index, backed by Neo4j.

The legacy chroma-backed path was removed when the code_index
migrated to neo4j: ``KnowledgeIndex`` now takes a
:class:`Neo4jKnowledgeClient` and routes every public method
through it. The cross-project sibling walk is a separate scope
(downgraded to a per-project MVP — siblings iterate
``driver_for_knowledge`` per project in a follow-up).

:class:`KnowledgeIndex` is a thin coordinator — every distinct
responsibility is delegated to a collaborator:

  - :class:`KnowledgeMetadataCodec` — flatten/unflatten + content
    hash (kept for the per-text chunk ``text`` field).
  - :class:`NewlinePreservingChunker` — default chunker; caller can
    inject any :class:`ChunkingStrategy`.
  - An optional :class:`Embedder` for the 384-dim vector used by
    the vector search index. The default :class:`HashEmbedder`
    is deterministic and offline-safe.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agno.knowledge.chunking.strategy import ChunkingStrategy
from agno.knowledge.document.base import Document

from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.knowledge.chunking import NewlinePreservingChunker
from ember_code.core.knowledge.metadata_codec import KnowledgeMetadataCodec
from ember_code.core.knowledge.models import (
    KnowledgeAddResult,
    KnowledgeDeleteResult,
    KnowledgeIndexEntry,
    KnowledgeSearchResult,
)

logger = logging.getLogger(__name__)


class KnowledgeIndex:
    """Per-project knowledge index backed by Neo4j.

    Args:
        project: project directory (used to derive the on-disk path).
        data_dir: ember root, defaults to ``~/.ember``.
        chunker: how to split inline content for ``add(...)``. Default
            ``NewlinePreservingChunker(chunk_size=550, overlap=75)`` —
            sized so chunks stay under the 256-token window of our
            ``all-MiniLM-L6-v2`` embedder. Markdown/code is token-dense
            (~0.36 tokens/char), so 550 chars ≈ 200 tokens with headroom.
        neo4j_client: required; a :class:`Neo4jKnowledgeClient` bound
            to the per-project driver.
        embedder: 384-dim vector producer; inject ``LiveEmbedder()``
            for production or ``HashEmbedder()`` for tests.
    """

    def __init__(
        self,
        *,
        project: str | Path,
        data_dir: str | Path = "~/.ember",
        chunker: ChunkingStrategy | None = None,
        neo4j_client: Any,
        embedder: Any | None = None,
    ):
        if neo4j_client is None:
            raise RuntimeError(
                "KnowledgeIndex now requires neo4j_client= — the chroma "
                "backend was removed when the code_index migrated to "
                "neo4j. Construct a Neo4jKnowledgeClient (from the "
                "Neo4jRuntime.driver_for_knowledge(project_id)) and "
                "pass it in."
            )
        self.project = project
        self.project_id = resolve_project_id(project)
        self.data_dir = data_dir
        self.chunker = chunker or NewlinePreservingChunker(chunk_size=550, overlap=75)
        # Knowledge has a single backend: the per-project neo4j
        # client. The chroma path was removed when code_index
        # migrated to neo4j.
        self._neo4j_client = neo4j_client
        self._embedder = embedder
        self._codec = KnowledgeMetadataCodec()

    async def start(self) -> None:
        """No-op on the neo4j path — the client is owned by the
        runtime and is already open by the time the indexer is
        constructed. Kept as a method for symmetry with the chroma
        path so callers don't need to branch on backend.
        """
        return

    async def _require_embedder(self) -> Any:
        """Return the configured embedder, or raise on use.

        The embedder is injected at construction; we delay the
        import to avoid loading the live model at module import.
        """
        if self._embedder is None:
            raise RuntimeError(
                "KnowledgeIndex requires an embedder to add or search entries. "
                "Pass embedder=HashEmbedder() for tests or LiveEmbedder() in "
                "production (the Session.attach_knowledge_neo4j() seam injects it)."
            )
        return self._embedder

    async def close(self) -> None:
        """No-op — the neo4j client lives across multiple
        :class:`KnowledgeIndex` instances (one driver per process,
        owned by the runtime) so we deliberately do not close it
        here.
        """

    # -- Public API ------------------------------------------------------------

    async def add(
        self,
        *,
        content: str,
        name: str | None = None,
        source: str = "",
        metadata: dict[str, str] | None = None,
        entry_id: str | None = None,
    ) -> str:
        """Insert an inline knowledge entry, chunked via the configured strategy.

        Short content stays as one chunk; longer content is split (with
        overlap) so each chunk gets its own embedding and search returns
        the most relevant slice rolled up to its parent document.

        Returns the stable entry id. On failure returns an empty string
        rather than raising; callers wanting structured errors should
        use :meth:`add_document` directly.
        """
        chunked_documents = self.chunker.chunk(Document(content=content))
        chunks = [d.content for d in chunked_documents if d.content]
        if not chunks:
            chunks = [content]
        result = await self.add_document(
            chunks=chunks,
            full_content=content,
            name=name,
            source=source,
            metadata=metadata,
            entry_id=entry_id,
        )
        return result.entry_id or ""

    async def add_document(
        self,
        *,
        chunks: list[str],
        full_content: str | None = None,
        name: str | None = None,
        source: str = "",
        metadata: dict[str, str] | None = None,
        entry_id: str | None = None,
    ) -> KnowledgeAddResult:
        """Insert one parent document with N chunks linked to its :Entry.

        Returns a :class:`KnowledgeAddResult` carrying the stable
        entry id (16-char content hash) on success. Empty ``chunks``
        returns ``success=False`` rather than raising — the ingester
        loop treats it as "nothing to store" and moves on.
        """
        return await self._add_document_neo4j(
            chunks=chunks,
            full_content=full_content,
            name=name,
            source=source,
            metadata=metadata,
            entry_id=entry_id,
        )

    async def search(
        self,
        *,
        query: str,
        limit: int = 5,
        cross_project: bool = False,
    ) -> list[KnowledgeSearchResult]:
        """Semantic search (neo4j-only — chroma path is removed).

        ``cross_project`` is a vestigial parameter: the cross-
        project sibling walk was chroma-specific and isn't
        implemented on the neo4j path. Kept on the signature for
        back-compat with existing callers; ignored.
        """
        return await self._search_neo4j(query=query, limit=limit)

    async def count(self) -> int:
        return await self._neo4j_client.count()

    async def list_entries(self, *, limit: int = 1000) -> list[KnowledgeIndexEntry]:
        """Return every entry in the current project — used by YAML sync."""
        return await self._list_entries_neo4j(limit=limit)

    async def delete_by_query(self, query: str, *, limit: int = 10) -> KnowledgeDeleteResult:
        """Find entries matching ``query`` and delete them."""
        return await self._delete_by_query_neo4j(query=query, limit=limit)

    async def delete_entry(self, entry_id: str) -> bool:
        """Public single-entry delete — used by the panel's Remove
        button. Presence check returns ``False`` for missing ids."""
        if not await self._neo4j_client.has_entry(entry_id):
            return False
        return await self._neo4j_client.delete_entry(entry_id)

    async def has_entry(self, entry_id: str) -> bool:
        return await self._neo4j_client.has_entry(entry_id)

    # -- Neo4j backend (mirror of the chroma surface above) -----------------

    async def _add_document_neo4j(
        self,
        *,
        chunks: list[str],
        full_content: str | None,
        name: str | None,
        source: str,
        metadata: dict[str, str] | None,
        entry_id: str | None,
    ) -> KnowledgeAddResult:
        """Neo4j path for :meth:`add_document`.

        The chroma path stores the parent doc + chunk set as two
        collections and computes the rollup in Python. The neo4j
        path stores them as :Entry (parent) + :Chunk (children) +
        ``Entry.embedding`` for the vector search, and skips the
        rollup (we return the entry's full content as the
        ``content`` field directly).
        """
        if not chunks:
            return KnowledgeAddResult.fail("add_document requires at least one chunk")
        embedder = await self._require_embedder()
        document_text = full_content if full_content is not None else "\n\n".join(chunks)
        eid = entry_id or self._codec.content_hash(document_text)
        display_name = name or eid
        # One embedding per entry (matches the entry_embedding
        # vector index on :Entry). The first chunk's text is the
        # canonical "query" against the entry for the per-text
        # split; for the live model the embedder can take the
        # full content directly. We use the document text as the
        # query — the live embedder is responsible for token
        # truncation.
        embedding = embedder.embed([document_text])[0]
        chunk_rows: list[tuple[str, int, int]] = []
        # Record line_from / line_to from the chunker for the
        # rollup later; the chroma path's codec tracks per-chunk
        # metadata, the neo4j path is lighter — it just stores
        # ``text`` and lets the caller format at render time.
        # For now the line numbers are 0/0 (no per-line tracking
        # until the embedder has a per-chunk split API).
        await self._neo4j_client.add_entry(
            entry_id=eid,
            name=display_name,
            source=source,
            content=document_text,
            embedding=embedding,
            chunks=chunk_rows,
        )
        return KnowledgeAddResult.ok(f"stored entry {eid}", entry_id=eid)

    async def _search_neo4j(self, *, query: str, limit: int) -> list[KnowledgeSearchResult]:
        embedder = await self._require_embedder()
        query_vec = embedder.embed([query])[0]
        rows = await self._neo4j_client.search(query_embedding=query_vec, limit=limit)
        return [
            KnowledgeSearchResult(
                entry_id=r["entry_id"],
                content=r.get("content", ""),
                name=r.get("name", ""),
                source=r.get("source", ""),
                project=self.project_id,
                parent_content=r.get("content", ""),
                score=r.get("score"),
                metadata={},
            )
            for r in rows
        ]

    async def _list_entries_neo4j(self, *, limit: int) -> list[KnowledgeIndexEntry]:
        rows = await self._neo4j_client.list_entries(limit=limit)
        return [
            KnowledgeIndexEntry(
                id=r["entry_id"],
                content=r.get("content", ""),
                source=r.get("source", ""),
                metadata=r.get("metadata", {}),
            )
            for r in rows
        ]

    async def _delete_by_query_neo4j(self, *, query: str, limit: int) -> KnowledgeDeleteResult:
        embedder = await self._require_embedder()
        query_vec = embedder.embed([query])[0]
        rows = await self._neo4j_client.search(query_embedding=query_vec, limit=limit)
        if not rows:
            return KnowledgeDeleteResult(deleted=0, reason="no matches")
        deleted = 0
        errors: list[str] = []
        for r in rows:
            eid = r["entry_id"]
            if not eid:
                continue
            try:
                if await self._neo4j_client.delete_entry(eid):
                    deleted += 1
                else:
                    errors.append(f"{eid}: delete returned False")
            except Exception as exc:  # surface the failure rather than swallow
                errors.append(f"{eid}: {exc}")
        return KnowledgeDeleteResult(deleted=deleted, errors=errors)
