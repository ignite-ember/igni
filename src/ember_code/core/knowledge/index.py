"""Per-project knowledge index, backed by ChromaDB.

Lives at ``~/.ember/projects/<project_id>/knowledge.chroma/``. Each
entry is stored as one parent row in ``knowledge_documents`` plus N
chunk rows in ``knowledge_chunks`` (linked via ``parent_doc_id``
metadata) so search can run against chunks and roll up to whole
documents. Lifecycle: lazy-connect on first use; caller owns ``close()``.

:class:`KnowledgeIndex` is a thin coordinator — every distinct
responsibility is delegated to a collaborator:

  - :class:`ChromaClientFactory` — client + collection lifecycle,
    HNSW config. Shared with :class:`CodeIndex` so the two indexes'
    recall settings can't drift.
  - :class:`KnowledgeStore` — composed :class:`DocumentsCollection` +
    :class:`ChunksCollection` pair plus the per-entry
    :meth:`delete_entry` atom returning a :class:`DeleteOutcome`.
  - :class:`KnowledgeMetadataCodec` — flatten/unflatten + content
    hash + typed :class:`ChunkMetadata` builder.
  - :class:`ChunkResultRollup` — chunk-search-to-parent-doc roll-up,
    reusable across local + sibling stores.
  - :class:`SiblingProjectSearcher` — cross-project search iterator.
  - :class:`NewlinePreservingChunker` — default chunker; caller can
    inject any :class:`ChunkingStrategy`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from agno.knowledge.chunking.strategy import ChunkingStrategy
from agno.knowledge.document.base import Document

from ember_code.core.code_index.chroma_client_factory import ChromaClientFactory
from ember_code.core.code_index.paths import knowledge_chroma_path
from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.knowledge.chunking import NewlinePreservingChunker
from ember_code.core.knowledge.collections import (
    ChunksCollection,
    DocumentsCollection,
)
from ember_code.core.knowledge.metadata_codec import KnowledgeMetadataCodec
from ember_code.core.knowledge.models import (
    KnowledgeAddResult,
    KnowledgeDeleteResult,
    KnowledgeIndexEntry,
    KnowledgeSearchResult,
)
from ember_code.core.knowledge.rollup import ChunkResultRollup
from ember_code.core.knowledge.sibling_search import SiblingProjectSearcher
from ember_code.core.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)

DOCUMENTS_COLLECTION = "knowledge_documents"
CHUNKS_COLLECTION = "knowledge_chunks"


class KnowledgeIndex:
    """Per-project knowledge index backed by ChromaDB.

    Args:
        project: project directory (used to derive the on-disk path).
        data_dir: ember root, defaults to ``~/.ember``.
        chunker: how to split inline content for ``add(...)``. Default
            ``NewlinePreservingChunker(chunk_size=550, overlap=75)`` —
            sized so chunks stay under the 256-token window of our
            ``all-MiniLM-L6-v2`` embedder. Markdown/code is token-dense
            (~0.36 tokens/char), so 550 chars ≈ 200 tokens with headroom.
    """

    def __init__(
        self,
        *,
        project: str | Path,
        data_dir: str | Path = "~/.ember",
        chunker: ChunkingStrategy | None = None,
        neo4j_client: Any | None = None,
        embedder: Any | None = None,
    ):
        self.project = project
        self.project_id = resolve_project_id(project)
        self.data_dir = data_dir
        self.chunker = chunker or NewlinePreservingChunker(chunk_size=550, overlap=75)
        self._factory = ChromaClientFactory()
        self._codec = KnowledgeMetadataCodec()
        self._store: KnowledgeStore | None = None
        self._lock = asyncio.Lock()
        self._rollup = ChunkResultRollup(codec=self._codec, project_label=self.project_id)
        self._sibling_searcher = SiblingProjectSearcher(
            factory=self._factory,
            codec=self._codec,
            current_project_id=self.project_id,
            data_dir=self.data_dir,
        )
        # Optional Neo4j backend. When set, ``add`` / ``search`` /
        # ``count`` / ``list_entries`` / ``delete_*`` / ``has_entry``
        # all route through ``Neo4jKnowledgeClient`` and the chroma
        # ``_store`` is never opened. The ``embedder`` parameter
        # supplies the 384-dim vector for ``add`` / ``search`` on
        # the neo4j path; the legacy chroma path uses its own
        # all-MiniLM-L6-v2 embedder via ``KnowledgeIndex.add``'s
        # ``embedding_fn`` kwarg.
        self._neo4j_client = neo4j_client
        self._embedder = embedder

    async def start(self) -> None:
        """Open the chroma client + collections. Idempotent.

        On the neo4j path the client is owned by the runtime and
        is already open — :meth:`start` is a no-op there (kept
        symmetric with the chroma path so callers don't need to
        branch on backend).
        """
        if self._neo4j_client is not None:
            return
        async with self._lock:
            if self._store is not None:
                return
            path = knowledge_chroma_path(self.project, data_dir=self.data_dir)
            path.mkdir(parents=True, exist_ok=True)
            client = await asyncio.to_thread(self._factory.open, path)
            docs_handle = await asyncio.to_thread(
                self._factory.get_or_create, client, DOCUMENTS_COLLECTION
            )
            chunks_handle = await asyncio.to_thread(
                self._factory.get_or_create, client, CHUNKS_COLLECTION
            )
            self._store = KnowledgeStore(
                docs=DocumentsCollection(docs_handle),
                chunks=ChunksCollection(chunks_handle),
            )

    async def close(self) -> None:
        """Drop the in-memory client. Persistent data stays on disk."""
        async with self._lock:
            self._store = None
        # ``_neo4j_client`` lives across multiple :class:`KnowledgeIndex`
        # instances (one driver per process, owned by the runtime)
        # so we deliberately do not close it here.

    async def _require_embedder(self) -> Any:
        """Return the configured embedder or raise — needed only on
        the neo4j path (the chroma path embeds via its own
        pipeline)."""
        if self._embedder is None:
            raise RuntimeError(
                "KnowledgeIndex constructed with neo4j_client= requires "
                "an embedder= parameter to vectorize queries + writes"
            )
        return self._embedder

    async def _ensure_started(self) -> KnowledgeStore:
        """Lazy-start hook returning the composed :class:`KnowledgeStore`.

        Callers use the returned store instead of ``self._store`` so
        downstream code doesn't need to reassert the not-``None``
        invariant on every method — the return type is
        unconditionally the concrete store.
        """
        if self._store is None:
            await self.start()
        assert self._store is not None
        return self._store

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
        """Insert one parent document with N chunks linked by ``parent_doc_id``.

        Returns a :class:`KnowledgeAddResult` carrying the stable entry
        id (16-char content hash) on success. Empty ``chunks`` returns
        ``success=False`` rather than raising — the ingester loop
        treats it as "nothing to store" and moves on.
        """
        if self._neo4j_client is not None:
            return await self._add_document_neo4j(
                chunks=chunks,
                full_content=full_content,
                name=name,
                source=source,
                metadata=metadata,
                entry_id=entry_id,
            )
        store = await self._ensure_started()
        if not chunks:
            return KnowledgeAddResult.fail("add_document requires at least one chunk")

        document_text = full_content if full_content is not None else "\n\n".join(chunks)
        eid = entry_id or self._codec.content_hash(document_text)
        display_name = name or eid

        # Upsert parent — metadata carries name/source/extra so
        # list_entries / sync can return the shape callers expect.
        # A zero-vector embedding skips the embedder: parents are
        # NEVER queried for similarity (search hits the chunks
        # collection and rolls up), so embedding 13k-char docs to a
        # truncated 256-token vector was wasted work + storage.
        doc_metadata = self._codec.flatten(
            entry_id=eid, name=display_name, source=source, extras=metadata
        )
        await store.docs.upsert_parent(entry_id=eid, document=document_text, metadata=doc_metadata)

        # Replace chunk set: codec builds one typed ChunkMetadata per
        # chunk; the collection wrapper flattens to dict + generates
        # the chunk id inside replace_for_parent.
        chunk_metadatas = [
            self._codec.flatten_chunk(
                entry_id=eid,
                chunk_index=i,
                name=display_name,
                source=source,
            )
            for i in range(len(chunks))
        ]
        await store.chunks.replace_for_parent(
            entry_id=eid, chunks=chunks, metadatas=chunk_metadatas
        )
        return KnowledgeAddResult.ok(f"stored entry {eid}", entry_id=eid)

    async def search(
        self,
        *,
        query: str,
        limit: int = 5,
        cross_project: bool = False,
    ) -> list[KnowledgeSearchResult]:
        """Semantic search.

        ``cross_project=False`` (default) hits the current project's
        collection only. ``cross_project=True`` iterates every other
        project's chroma file and merges results by score.

        On the neo4j path the cross-project sibling walk isn't
        implemented yet (every project has its own knowledge DB on
        its own runtime subprocess; iterating every other process
        is a separate ticket). The per-project search returns the
        same ``KnowledgeSearchResult`` shape the chroma path does.
        """
        if self._neo4j_client is not None:
            return await self._search_neo4j(query=query, limit=limit)
        store = await self._ensure_started()
        results = await self._rollup.top_k(store, query=query, limit=limit)
        if not cross_project:
            return results

        results.extend(await self._sibling_searcher.search_all(query=query, limit=limit))
        results.sort(key=lambda r: r.score or 0.0, reverse=True)
        return results[:limit]

    async def count(self) -> int:
        if self._neo4j_client is not None:
            return await self._neo4j_client.count()
        store = await self._ensure_started()
        return await store.docs.count()

    async def list_entries(self, *, limit: int = 1000) -> list[KnowledgeIndexEntry]:
        """Return every entry in the current project — used by YAML sync."""
        if self._neo4j_client is not None:
            return await self._list_entries_neo4j(limit=limit)
        store = await self._ensure_started()
        page = await store.docs.get_all(limit=limit)
        return [
            KnowledgeIndexEntry(
                id=entry_id,
                content=content or "",
                source=(meta or {}).get("source", ""),
                metadata=self._codec.unflatten(meta or {}),
            )
            for entry_id, content, meta in page.rows()
        ]

    async def delete_by_query(self, query: str, *, limit: int = 10) -> KnowledgeDeleteResult:
        """Find entries matching ``query`` and delete them."""
        if self._neo4j_client is not None:
            return await self._delete_by_query_neo4j(query=query, limit=limit)
        store = await self._ensure_started()
        results = await self._rollup.top_k(store, query=query, limit=limit)
        if not results:
            return KnowledgeDeleteResult(deleted=0, reason="no matches")
        deleted = 0
        errors: list[str] = []
        for r in results:
            eid = r.entry_id
            if not eid:
                continue
            outcome = await store.delete_entry(eid)
            if outcome.ok:
                deleted += 1
            else:
                errors.append(f"{outcome.entry_id}: {outcome.error}")
        return KnowledgeDeleteResult(deleted=deleted, errors=errors)

    async def delete_entry(self, entry_id: str) -> bool:
        """Public single-entry delete — used by the panel's Remove
        button. Wraps :meth:`KnowledgeStore.delete_entry` with a
        presence check so a missing id returns ``False`` instead of
        succeeding-on-nothing."""
        if self._neo4j_client is not None:
            if not await self._neo4j_client.has_entry(entry_id):
                return False
            return await self._neo4j_client.delete_entry(entry_id)
        store = await self._ensure_started()
        if not await store.docs.exists(entry_id):
            return False
        outcome = await store.delete_entry(entry_id)
        return outcome.ok

    async def has_entry(self, entry_id: str) -> bool:
        if self._neo4j_client is not None:
            return await self._neo4j_client.has_entry(entry_id)
        store = await self._ensure_started()
        return await store.docs.exists(entry_id)

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
