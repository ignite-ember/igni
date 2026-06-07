"""Per-project knowledge index, backed by ChromaDB.

Lives at ``~/.ember/projects/<project_id>/knowledge.chroma/``. Each
entry is stored as one parent row in ``knowledge_documents`` plus N
chunk rows in ``knowledge_chunks`` (linked via ``parent_doc_id``
metadata) so search can run against chunks and roll up to whole
documents. Lifecycle: lazy-connect on first use; caller owns ``close()``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agno.knowledge.chunking.recursive import RecursiveChunking
from agno.knowledge.chunking.strategy import ChunkingStrategy
from agno.knowledge.document.base import Document

from ember_code.core.code_index.paths import (
    data_root,
    knowledge_chroma_path,
)
from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.embeddings import EmbeddingFunction

logger = logging.getLogger(__name__)

DOCUMENTS_COLLECTION = "knowledge_documents"
CHUNKS_COLLECTION = "knowledge_chunks"


class KnowledgeIndex:
    """Per-project knowledge index backed by ChromaDB.

    Args:
        project: project directory (used to derive the on-disk path).
        data_dir: ember root, defaults to ``~/.ember``.
        chunker: how to split inline content for ``add(...)``. Default
            ``RecursiveChunking(chunk_size=800, overlap=100)`` — sized
            for our 384-dim ``all-MiniLM-L6-v2`` embedder.
    """

    def __init__(
        self,
        *,
        project: str | Path,
        data_dir: str | Path = "~/.ember",
        chunker: ChunkingStrategy | None = None,
    ):
        self.project = project
        self.project_id = resolve_project_id(project)
        self.data_dir = data_dir
        self.chunker = chunker or RecursiveChunking(chunk_size=800, overlap=100)
        self._client: Any | None = None
        self._docs: Any | None = None
        self._chunks: Any | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Open the chroma client + collections. Idempotent."""
        async with self._lock:
            if self._client is not None:
                return
            path = knowledge_chroma_path(self.project, data_dir=self.data_dir)
            path.mkdir(parents=True, exist_ok=True)
            self._client = await asyncio.to_thread(_open_client, path)
            self._docs = await asyncio.to_thread(
                _get_or_create_collection, self._client, DOCUMENTS_COLLECTION
            )
            self._chunks = await asyncio.to_thread(
                _get_or_create_collection, self._client, CHUNKS_COLLECTION
            )

    async def close(self) -> None:
        """Drop the in-memory client. Persistent data stays on disk."""
        async with self._lock:
            self._client = None
            self._docs = None
            self._chunks = None

    async def _ensure_started(self) -> None:
        if self._client is None:
            await self.start()

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
        """
        chunked_documents = self.chunker.chunk(Document(content=content))
        chunks = [d.content for d in chunked_documents if d.content]
        if not chunks:
            chunks = [content]
        return await self.add_document(
            chunks=chunks,
            full_content=content,
            name=name,
            source=source,
            metadata=metadata,
            entry_id=entry_id,
        )

    async def add_document(
        self,
        *,
        chunks: list[str],
        full_content: str | None = None,
        name: str | None = None,
        source: str = "",
        metadata: dict[str, str] | None = None,
        entry_id: str | None = None,
    ) -> str:
        """Insert one parent document with N chunks linked by ``parent_doc_id``.

        Returns the stable entry id (16-char content hash).
        """
        await self._ensure_started()
        if not chunks:
            raise ValueError("add_document requires at least one chunk")

        document_text = full_content if full_content is not None else "\n\n".join(chunks)
        eid = entry_id or _content_hash(document_text)

        # Upsert parent — embedding from the full content; metadata
        # carries name/source/extra so list_entries / sync can return
        # the dict shape callers expect.
        doc_metadata = _flatten_metadata(
            entry_id=eid, name=name or eid, source=source, extras=metadata
        )
        await asyncio.to_thread(
            self._docs.upsert,
            ids=[eid],
            documents=[document_text],
            metadatas=[doc_metadata],
        )

        # Replace chunk set: delete prior chunks for this doc, then upsert new ones.
        await asyncio.to_thread(self._chunks.delete, where={"parent_doc_id": eid})
        chunk_ids = [f"{eid}::{i}" for i in range(len(chunks))]
        chunk_metadatas = [
            {
                "parent_doc_id": eid,
                "chunk_index": i,
                "name": name or eid,
                "source": source,
            }
            for i in range(len(chunks))
        ]
        await asyncio.to_thread(
            self._chunks.upsert,
            ids=chunk_ids,
            documents=chunks,
            metadatas=chunk_metadatas,
        )
        return eid

    async def search(
        self,
        *,
        query: str,
        limit: int = 5,
        cross_project: bool = False,
    ) -> list[dict]:
        """Semantic search.

        ``cross_project=False`` (default) hits the current project's
        collection only. ``cross_project=True`` iterates every other
        project's chroma file and merges results by score.
        """
        await self._ensure_started()
        results = await self._search_local(query=query, limit=limit)
        if not cross_project:
            return results

        # Pull from sibling projects too — open a quick read-only client per file.
        for sibling_path in _iter_sibling_chroma_paths(
            data_dir=self.data_dir, current_id=self.project_id
        ):
            sibling = await asyncio.to_thread(_open_client, sibling_path)
            chunks_coll = await asyncio.to_thread(
                _get_or_create_collection, sibling, CHUNKS_COLLECTION
            )
            docs_coll = await asyncio.to_thread(
                _get_or_create_collection, sibling, DOCUMENTS_COLLECTION
            )
            sibling_results = await self._roll_up_chunks(
                query=query,
                limit=limit,
                chunks_coll=chunks_coll,
                docs_coll=docs_coll,
                project_label=sibling_path.parent.name,
            )
            results.extend(sibling_results)

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    async def count(self) -> int:
        await self._ensure_started()
        return await asyncio.to_thread(self._docs.count)

    async def list_entries(self, *, limit: int = 1000) -> list[dict]:
        """Return every entry in the current project — used by YAML sync."""
        await self._ensure_started()
        page = await asyncio.to_thread(
            self._docs.get,
            limit=limit,
            include=["documents", "metadatas"],
        )
        entries: list[dict] = []
        for entry_id, content, meta in zip(
            page.get("ids", []) or [],
            page.get("documents", []) or [],
            page.get("metadatas", []) or [],
            strict=False,
        ):
            entries.append(
                {
                    "id": entry_id,
                    "content": content or "",
                    "source": (meta or {}).get("source", ""),
                    "metadata": _unflatten_metadata(meta or {}),
                }
            )
        return entries

    async def delete_by_query(self, query: str, *, limit: int = 10) -> int:
        """Find entries matching ``query`` and delete them. Returns count deleted."""
        await self._ensure_started()
        results = await self._search_local(query=query, limit=limit)
        if not results:
            return 0
        deleted = 0
        for r in results:
            eid = r.get("entry_id")
            if not eid:
                continue
            try:
                await self._delete_doc(eid)
                deleted += 1
            except Exception:
                logger.exception("delete failed for %s", eid)
        return deleted

    async def has_entry(self, entry_id: str) -> bool:
        await self._ensure_started()
        result = await asyncio.to_thread(self._docs.get, ids=[entry_id], include=[])
        return bool(result.get("ids"))

    # -- Internal --------------------------------------------------------------

    async def _delete_doc(self, entry_id: str) -> None:
        await asyncio.to_thread(self._docs.delete, ids=[entry_id])
        await asyncio.to_thread(self._chunks.delete, where={"parent_doc_id": entry_id})

    async def _search_local(self, *, query: str, limit: int) -> list[dict]:
        return await self._roll_up_chunks(
            query=query,
            limit=limit,
            chunks_coll=self._chunks,
            docs_coll=self._docs,
            project_label=self.project_id,
        )

    async def _roll_up_chunks(
        self,
        *,
        query: str,
        limit: int,
        chunks_coll: Any,
        docs_coll: Any,
        project_label: str,
    ) -> list[dict]:
        """Query chunks, dedupe by parent doc, fetch parent metadata."""
        if await asyncio.to_thread(chunks_coll.count) == 0:
            return []
        # Over-query so we have enough unique parents after dedup.
        n = max(limit * 4, limit)
        chunk_results = await asyncio.to_thread(
            chunks_coll.query,
            query_texts=[query],
            n_results=n,
            include=["documents", "metadatas", "distances"],
        )
        ids_groups = chunk_results.get("ids") or [[]]
        docs_groups = chunk_results.get("documents") or [[]]
        metas_groups = chunk_results.get("metadatas") or [[]]
        dists_groups = chunk_results.get("distances") or [[]]
        if not ids_groups or not ids_groups[0]:
            return []

        # Best chunk per parent wins; preserve order by score.
        best: dict[str, dict] = {}
        for _i, doc, meta, dist in zip(
            ids_groups[0],
            docs_groups[0],
            metas_groups[0],
            dists_groups[0],
            strict=False,
        ):
            parent_id = (meta or {}).get("parent_doc_id")
            if not parent_id:
                continue
            score = 1.0 - float(dist) if dist is not None else 0.0
            current = best.get(parent_id)
            if current is None or score > current["score"]:
                best[parent_id] = {
                    "score": score,
                    "chunk": doc or "",
                    "chunk_meta": meta or {},
                }

        if not best:
            return []

        # Pull parent rows for the matched parents.
        parent_ids = list(best.keys())
        docs_page = await asyncio.to_thread(
            docs_coll.get,
            ids=parent_ids,
            include=["documents", "metadatas"],
        )
        parent_rows = {
            pid: (text or "", meta or {})
            for pid, text, meta in zip(
                docs_page.get("ids", []) or [],
                docs_page.get("documents", []) or [],
                docs_page.get("metadatas", []) or [],
                strict=False,
            )
        }

        results: list[dict] = []
        for parent_id, entry in best.items():
            parent_doc, parent_meta = parent_rows.get(parent_id, ("", {}))
            content = entry["chunk"]
            truncated = content[:1000] + "..." if len(content) > 1000 else content
            results.append(
                {
                    "entry_id": parent_id,
                    "content": truncated,
                    "name": parent_meta.get("name", ""),
                    "source": parent_meta.get("source", ""),
                    "score": entry["score"],
                    "project": project_label,
                    "metadata": _unflatten_metadata(parent_meta),
                    "parent_content": parent_doc,
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]


# -- Helpers ------------------------------------------------------------------


_chroma_lock = threading.Lock()


def _open_client(path: Path) -> Any:
    """Open a chromadb persistent client at ``path``.

    Wrapped in a module-level lock — chromadb's client cache is per-process
    and not thread-safe during construction.
    """
    import chromadb

    with _chroma_lock:
        return chromadb.PersistentClient(path=str(path))


def _get_or_create_collection(client: Any, name: str) -> Any:
    """Mirror CodeIndex's high-recall HNSW config.

    Chroma defaults to ``hnsw:search_ef=10`` which silently caps recall
    at any ``top_k > ~10`` — the index returns near-floor matches
    instead of the actually-closest neighbors. The knowledge base is
    expected to scale to 10k+ entries, and the agent expects every
    item to be considered against the query, so we lift ``search_ef``
    to 10000 (effectively-exact at our scale) and raise ``M`` /
    ``construction_ef`` to give the graph the topology that lets a
    high ``search_ef`` actually pay off.

    Kept in lockstep with ``code_index.index._get_or_create_collection``
    and ``scripts/reindex_hnsw.py`` (TARGET_HNSW_METADATA) — bump all
    three together or recall regresses on one of the two indexes.

    Existing collections created without this metadata keep chroma's
    defaults until rebuilt; ``scripts/reindex_hnsw.py`` handles the
    in-place migration.
    """
    return client.get_or_create_collection(
        name=name,
        embedding_function=EmbeddingFunction(),
        metadata={
            "hnsw:space": "cosine",
            "hnsw:M": 32,
            "hnsw:construction_ef": 400,
            "hnsw:search_ef": 10000,
        },
    )


def _iter_sibling_chroma_paths(*, data_dir: str | Path, current_id: str) -> Iterable[Path]:
    """Yield ``knowledge.chroma`` paths for every project except the current one."""
    projects_dir = data_root(data_dir) / "projects"
    if not projects_dir.is_dir():
        return
    for entry in projects_dir.iterdir():
        if not entry.is_dir() or entry.name == current_id:
            continue
        chroma_path = entry / "knowledge.chroma"
        if chroma_path.is_dir():
            yield chroma_path


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _flatten_metadata(
    *,
    entry_id: str,
    name: str,
    source: str,
    extras: dict[str, str] | None = None,
) -> dict[str, str]:
    """ChromaDB requires flat scalar metadata — encode extras with a prefix.

    Reverse of :func:`_unflatten_metadata`.
    """
    out: dict[str, str] = {
        "entry_id": entry_id,
        "name": name,
        "source": source,
    }
    for k, v in (extras or {}).items():
        if k and v is not None:
            out[f"meta.{k}"] = str(v)
    return out


def _unflatten_metadata(flat: dict[str, str]) -> dict[str, str]:
    """Pull ``meta.<k>`` keys back into a nested dict (reverse of flatten)."""
    out: dict[str, str] = {}
    for k, v in (flat or {}).items():
        if k.startswith("meta."):
            out[k[len("meta.") :]] = str(v)
    return out
