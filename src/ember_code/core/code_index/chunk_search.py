"""Semantic chunk search — the shared engine behind ``search`` and ``search_among``.

``CodeIndex.search`` and ``CodeIndex.search_among`` used to be ~100 LoC
each of near-identical code that differed only in how they built the
chunk-side ``where`` clause. This class collapses both into one
:meth:`execute` call; the two ``CodeIndex`` methods become thin
wrappers that build the where-clause and delegate.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from ember_code.core.code_index.chroma_codec import ChromaRowCodec
from ember_code.core.code_index.schema.chroma_row import ChromaGetPage, ChromaQueryPage
from ember_code.core.code_index.schema.items import CodeIndexResult


class _BestChunkHit(BaseModel):
    """Best-scoring chunk for one parent document during semantic search.

    Implementation-local scratch model — never crosses a public API
    boundary. Kept here (not in ``schema/``) intentionally.
    """

    score: float
    chunk_text: str
    chunk_id: str


class ChunkSearch:
    """Runs a semantic chunk query and dedupes to parent doc results.

    Constructed once per :class:`CodeIndex`; :meth:`execute` per query.
    """

    #: Semantic-search preview cap — chunks longer than this are truncated
    #: with a trailing ellipsis so the agent doesn't get pages of raw
    #: source pasted into every hit.
    PREVIEW_MAX_CHARS: int = 1000

    #: Hard cap for the parent-doc filter resolution (see
    #: :meth:`resolve_parent_ids`). Beyond this the caller really wants
    #: ``filter_items``, not a semantic search inside an enormous
    #: candidate set.
    PARENT_ID_CAP: int = 10_000

    def __init__(self, codec: ChromaRowCodec):
        self._codec = codec

    async def resolve_parent_ids(self, docs: Any, where: dict[str, Any]) -> list[str]:
        """Find parent doc IDs matching a metadata ``where`` filter.

        Used to translate a quality-field filter (which lives on parent
        docs) into a chunk-side filter (which can only match the
        denormalized columns on chunk metadata). Capped at
        :attr:`PARENT_ID_CAP` IDs.
        """
        page_raw = await asyncio.to_thread(
            docs.get,
            where=where,
            limit=self.PARENT_ID_CAP,
            include=[],
        )
        page = ChromaGetPage.from_chroma(page_raw)
        return list(page.ids)

    async def execute(
        self,
        *,
        docs: Any,
        chunks: Any,
        sha: str,
        query: str,
        chunk_where: dict[str, Any] | None,
        limit: int,
    ) -> list[CodeIndexResult]:
        """Run the chunk query, dedupe by parent doc, materialize results.

        Returns ``[]`` when the chunk query has no results or the
        chunks collection is empty. The caller is responsible for the
        empty-commit and no-chroma-dir guards; this method assumes both
        collections are open.
        """
        if await asyncio.to_thread(chunks.count) == 0:
            return []
        n = max(limit * 4, limit)

        query_kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": n,
            "include": ["documents", "metadatas", "distances"],
        }
        if chunk_where is not None:
            query_kwargs["where"] = chunk_where
        raw = await asyncio.to_thread(chunks.query, **query_kwargs)
        page = ChromaQueryPage.from_chroma(raw)
        ids, chunk_docs, chunk_metas, dists = page.row(0)
        if not ids:
            return []

        best = self._dedupe_by_parent(ids, chunk_docs, chunk_metas, dists)
        if not best:
            return []

        parent_ids = list(best.keys())
        parents_raw = await asyncio.to_thread(
            docs.get, ids=parent_ids, include=["documents", "metadatas"]
        )
        parents = ChromaGetPage.from_chroma(parents_raw)
        parent_rows = {
            pid: (text or "", meta or {})
            for pid, text, meta in zip(
                parents.ids, parents.documents, parents.metadatas, strict=False
            )
        }

        out: list[CodeIndexResult] = []
        for parent_id, hit in best.items():
            content_text, parent_meta = parent_rows.get(parent_id, ("", {}))
            preview = hit.chunk_text
            truncated = (
                preview[: self.PREVIEW_MAX_CHARS] + "..."
                if len(preview) > self.PREVIEW_MAX_CHARS
                else preview
            )
            out.append(
                self._codec.parse(
                    parent_id,
                    parent_meta,
                    sha,
                    content=content_text,
                    score=hit.score,
                    chunk_preview=truncated,
                )
            )
        out.sort(key=lambda r: r.score or 0.0, reverse=True)
        return out[:limit]

    def _dedupe_by_parent(
        self,
        ids: list[str],
        chunk_docs: list[str | None],
        chunk_metas: list[dict[str, Any] | None],
        dists: list[float | None],
    ) -> dict[str, _BestChunkHit]:
        """Keep the best-scoring chunk per parent document."""
        best: dict[str, _BestChunkHit] = {}
        for chunk_id, doc_text, meta, dist in zip(
            ids, chunk_docs, chunk_metas, dists, strict=False
        ):
            parent_id = (meta or {}).get("parent_doc_id")
            if not parent_id:
                continue
            score = 1.0 - float(dist) if dist is not None else 0.0
            current = best.get(parent_id)
            if current is None or score > current.score:
                best[parent_id] = _BestChunkHit(
                    score=score,
                    chunk_text=doc_text or "",
                    chunk_id=chunk_id,
                )
        return best
