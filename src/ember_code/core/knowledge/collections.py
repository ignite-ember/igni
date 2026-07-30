"""Typed wrappers around raw ChromaDB collection handles.

The raw ``chromadb`` collection objects are ``Any``-typed at our seams;
sprinkling ``asyncio.to_thread(self._docs.upsert, ...)`` throughout
:class:`KnowledgeIndex` both leaks the typing hole and blurs which
operations we actually depend on. These two wrappers pin the surface
to exactly the ops the index needs, and own the ``asyncio.to_thread``
offload once — so the index reads as async-native.

Read paths return typed :class:`ChromaQueryPage` / :class:`ChromaGetPage`
Pydantic wrappers instead of the raw ``dict[str, Any]`` chroma hands
back, so the ``Any`` at the seam is contained to one line per method.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ember_code.core.embeddings import EMBEDDING_DIMENSIONS
from ember_code.core.knowledge.metadata_codec import ChunkMetadata
from ember_code.core.knowledge.models import (
    ChromaGetPage,
    ChromaQueryPage,
)

# Sentinel embedding for parent documents — parents are never queried
# for similarity (search hits the chunks collection and rolls up), so
# spending an embedder pass on 13k-char parent docs is wasted work.
_PARENT_EMBEDDING: list[float] = [0.0] * EMBEDDING_DIMENSIONS


class DocumentsCollection:
    """Async-facing view of the ``knowledge_documents`` chroma handle.

    Wraps every write with ``asyncio.to_thread`` so callers stay on
    the event loop. Read paths that don't touch the embedder (``get``,
    ``count``) get the same treatment for uniformity.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    async def upsert_parent(
        self,
        *,
        entry_id: str,
        document: str,
        metadata: dict[str, str],
    ) -> None:
        """Upsert one parent doc with a zero-vector embedding placeholder."""
        await asyncio.to_thread(
            self._handle.upsert,
            ids=[entry_id],
            documents=[document],
            embeddings=[list(_PARENT_EMBEDDING)],
            metadatas=[metadata],
        )

    async def get_by_ids(
        self, entry_ids: list[str], *, include: list[str] | None = None
    ) -> ChromaGetPage:
        raw: dict[str, Any] = await asyncio.to_thread(
            self._handle.get,
            ids=entry_ids,
            include=include if include is not None else [],
        )
        return ChromaGetPage.from_chroma(raw)

    async def get_all(self, *, limit: int) -> ChromaGetPage:
        raw: dict[str, Any] = await asyncio.to_thread(
            self._handle.get,
            limit=limit,
            include=["documents", "metadatas"],
        )
        return ChromaGetPage.from_chroma(raw)

    async def delete(self, entry_id: str) -> None:
        await asyncio.to_thread(self._handle.delete, ids=[entry_id])

    async def count(self) -> int:
        return await asyncio.to_thread(self._handle.count)

    async def exists(self, entry_id: str) -> bool:
        page = await self.get_by_ids([entry_id])
        return bool(page.ids)


class ChunksCollection:
    """Async-facing view of the ``knowledge_chunks`` chroma handle."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    async def replace_for_parent(
        self,
        *,
        entry_id: str,
        chunks: list[str],
        metadatas: list[ChunkMetadata],
    ) -> None:
        """Drop the parent's existing chunks and upsert the new set.

        ``metadatas`` is a typed :class:`ChunkMetadata` list — the
        wrapper flattens each entry to Chroma's ``dict[str, str]``
        shape internally, so the ``dict[str, object]`` seam stays
        closed at the boundary of :class:`KnowledgeIndex`. Chunk-id
        generation (``"{entry_id}::{i}"``) is *this* method's
        responsibility, not the codec's.
        """
        await asyncio.to_thread(self._handle.delete, where={"parent_doc_id": entry_id})
        chunk_ids = [f"{entry_id}::{i}" for i in range(len(chunks))]
        await asyncio.to_thread(
            self._handle.upsert,
            ids=chunk_ids,
            documents=chunks,
            metadatas=[m.to_chroma_dict() for m in metadatas],
        )

    async def query(self, *, query_text: str, n_results: int) -> ChromaQueryPage:
        raw: dict[str, Any] = await asyncio.to_thread(
            self._handle.query,
            query_texts=[query_text],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        return ChromaQueryPage.from_chroma(raw)

    async def delete_by_parent(self, entry_id: str) -> None:
        await asyncio.to_thread(self._handle.delete, where={"parent_doc_id": entry_id})

    async def count(self) -> int:
        return await asyncio.to_thread(self._handle.count)
