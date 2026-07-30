"""Chunk-search roll-up for the knowledge index.

Chunks are what get embedded and queried; parent documents are what
callers actually want back. :class:`ChunkResultRollup` owns that
translation — query a chunks collection, dedupe hits down to
best-per-parent, fetch parent metadata in one batch, hand back a
sorted list of :class:`KnowledgeSearchResult`.

Split out of :class:`KnowledgeIndex` so the same rollup instance can
serve the local project *and* every sibling project during a
``cross_project=True`` search — the store is passed per-call so no
mutable-instance-state-per-sibling drift is possible.
"""

from __future__ import annotations

from ember_code.core.knowledge.metadata_codec import KnowledgeMetadataCodec
from ember_code.core.knowledge.models import (
    ChromaQueryPage,
    KnowledgeSearchResult,
    ParentRow,
    _BestChunkForParent,
)
from ember_code.core.knowledge.store import KnowledgeStore


class ChunkResultRollup:
    """Query chunks, dedupe by parent doc, fetch parent metadata.

    Stateless-across-calls by design — the :class:`KnowledgeStore`
    lives in the argument list of :meth:`top_k`, not on the instance,
    so one rollup can serve local + sibling searches interchangeably
    without any per-search reset ritual.
    """

    def __init__(
        self,
        *,
        codec: KnowledgeMetadataCodec,
        project_label: str,
    ) -> None:
        self._codec = codec
        self._project_label = project_label

    async def top_k(
        self,
        store: KnowledgeStore,
        *,
        query: str,
        limit: int,
    ) -> list[KnowledgeSearchResult]:
        """Search chunks in ``store``, roll up to parents, return top-``limit``."""
        if await store.chunks.count() == 0:
            return []

        # Over-query so we have enough unique parents after dedup.
        n = max(limit * 4, limit)
        page = await store.chunks.query(query_text=query, n_results=n)
        if page.is_empty:
            return []

        best = self._best_per_parent(page)
        if not best:
            return []

        parents = await self._fetch_parents(store, list(best.keys()))
        return self._build_results(best, parents, limit=limit)

    def _best_per_parent(self, page: ChromaQueryPage) -> dict[str, _BestChunkForParent]:
        """Highest-score chunk wins per parent doc."""
        best: dict[str, _BestChunkForParent] = {}
        for _id, doc, meta, dist in page.first_row_iter():
            parent_id = (meta or {}).get("parent_doc_id")
            if not parent_id:
                continue
            score = 1.0 - float(dist) if dist is not None else 0.0
            current = best.get(parent_id)
            if current is None or score > current.score:
                best[parent_id] = _BestChunkForParent(
                    score=score,
                    chunk=doc or "",
                    chunk_meta={str(k): str(v) for k, v in (meta or {}).items()},
                )
        return best

    async def _fetch_parents(
        self, store: KnowledgeStore, parent_ids: list[str]
    ) -> dict[str, ParentRow]:
        """Batch-fetch parent rows for the matched ids."""
        docs_page = await store.docs.get_by_ids(parent_ids, include=["documents", "metadatas"])
        return {
            pid: ParentRow(
                document=text or "",
                metadata={str(k): str(v) for k, v in (meta or {}).items()},
            )
            for pid, text, meta in docs_page.rows()
        }

    def _build_results(
        self,
        best: dict[str, _BestChunkForParent],
        parents: dict[str, ParentRow],
        *,
        limit: int,
    ) -> list[KnowledgeSearchResult]:
        """Assemble the final :class:`KnowledgeSearchResult` list."""
        results: list[KnowledgeSearchResult] = []
        for parent_id, entry in best.items():
            parent = parents.get(parent_id, ParentRow())
            content = entry.chunk
            truncated = content[:1000] + "..." if len(content) > 1000 else content
            results.append(
                KnowledgeSearchResult(
                    entry_id=parent_id,
                    content=truncated,
                    name=parent.metadata.get("name", ""),
                    source=parent.metadata.get("source", ""),
                    score=entry.score,
                    project=self._project_label,
                    metadata=self._codec.unflatten(parent.metadata),
                    parent_content=parent.document,
                )
            )
        results.sort(key=lambda r: r.score or 0.0, reverse=True)
        return results[:limit]
