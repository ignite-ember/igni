"""Cross-project knowledge search.

When :meth:`KnowledgeIndex.search` runs with ``cross_project=True``,
we want hits from every *other* project's chroma file too. That
concern splits cleanly off :class:`KnowledgeIndex` — the index only
needs to know "give me sibling results for this query"; it doesn't
need to know we iterate ``~/.ember/projects/*``, open a read-only
client per sibling, or wrap each handle in a fresh
:class:`KnowledgeStore`.

:class:`SiblingProjectSearcher` owns that pipeline. It's stateless
across calls (client-per-call, no cache) — matches the current
behaviour, and if cross-project ever hits a hot loop this class is
the natural home for a client cache.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

from ember_code.core.code_index.chroma_client_factory import ChromaClientFactory
from ember_code.core.code_index.paths import data_root
from ember_code.core.knowledge.collections import (
    ChunksCollection,
    DocumentsCollection,
)
from ember_code.core.knowledge.metadata_codec import KnowledgeMetadataCodec
from ember_code.core.knowledge.models import KnowledgeSearchResult
from ember_code.core.knowledge.rollup import ChunkResultRollup
from ember_code.core.knowledge.store import KnowledgeStore

DOCUMENTS_COLLECTION = "knowledge_documents"
CHUNKS_COLLECTION = "knowledge_chunks"


class SiblingProjectSearcher:
    """Runs a chunk-search across every non-current-project chroma file."""

    def __init__(
        self,
        *,
        factory: ChromaClientFactory,
        codec: KnowledgeMetadataCodec,
        current_project_id: str,
        data_dir: str | Path,
    ) -> None:
        self._factory = factory
        self._codec = codec
        self._current_project_id = current_project_id
        self._data_dir = data_dir

    async def search_all(self, *, query: str, limit: int) -> list[KnowledgeSearchResult]:
        """Return chunked-and-rolled-up hits from every sibling project."""
        results: list[KnowledgeSearchResult] = []
        for sibling_path in self._iter_sibling_chroma_paths():
            store = await self._open_sibling_store(sibling_path)
            rollup = ChunkResultRollup(
                codec=self._codec,
                project_label=sibling_path.parent.name,
            )
            sibling_results = await rollup.top_k(store, query=query, limit=limit)
            results.extend(sibling_results)
        return results

    async def _open_sibling_store(self, chroma_path: Path) -> KnowledgeStore:
        """Open a read-only client for one sibling, wrap in a store."""
        client = await asyncio.to_thread(self._factory.open, chroma_path)
        chunks_handle = await asyncio.to_thread(
            self._factory.get_or_create, client, CHUNKS_COLLECTION
        )
        docs_handle = await asyncio.to_thread(
            self._factory.get_or_create, client, DOCUMENTS_COLLECTION
        )
        return KnowledgeStore(
            docs=DocumentsCollection(docs_handle),
            chunks=ChunksCollection(chunks_handle),
        )

    def _iter_sibling_chroma_paths(self) -> Iterable[Path]:
        """Yield ``knowledge.chroma`` paths for every project except current."""
        projects_dir = data_root(self._data_dir) / "projects"
        if not projects_dir.is_dir():
            return
        for entry in projects_dir.iterdir():
            if not entry.is_dir() or entry.name == self._current_project_id:
                continue
            chroma_path = entry / "knowledge.chroma"
            if chroma_path.is_dir():
                yield chroma_path
