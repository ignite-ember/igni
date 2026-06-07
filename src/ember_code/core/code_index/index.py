"""Per-project, per-commit code index backed by ChromaDB.

Each commit gets its own ``<sha>.chroma/`` directory under
``~/.ember/projects/<project_id>/code_index/``. Indexing a new commit
copies the parent commit's directory in place, then applies the diff
on top — so each commit is fully self-contained but only the changed
files re-embed.

Lifecycle:

- :meth:`prepare_commit` — copy parent → child (or create empty), update manifest.
- :meth:`apply_delta` — apply a JSONL of file-level changes.
- :meth:`set_head` — point the manifest's ``head`` at a commit.
- :meth:`search` / :meth:`get_item` — query a commit (defaults to head).
- :meth:`clean` — drop commits not referenced by any branch and idle > N days.

Quality / category metadata are first-class typed chroma fields — each
quality dimension is its own indexed string column, each multi-value
category is its own ``\\x1f``-bracketed string. There is no ``tags``
field; the ``codeindex_query`` tool builds typed where-clauses from
its enum args without any string-tag parsing.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agno.knowledge.chunking.recursive import RecursiveChunking
from agno.knowledge.chunking.strategy import ChunkingStrategy
from agno.knowledge.document.base import Document
from pydantic import BaseModel

from ember_code.core.code_index.manifest import Manifest
from ember_code.core.code_index.paths import (
    commit_chroma_path,
)
from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.code_index.schema.items import CodeIndexItem, CodeIndexResult
from ember_code.core.embeddings import EmbeddingFunction

logger = logging.getLogger(__name__)

DOCUMENTS_COLLECTION = "code_index_documents"
CHUNKS_COLLECTION = "code_index_chunks"

# ASCII unit separator — used to bracket multi-value list fields so
# ``$contains: "\x1fsql-injection\x1f"`` exact-matches without false
# prefix collisions (``"sql"`` would otherwise match ``"sql-injection"``).
_LIST_SEP = "\x1f"

# Quality categorical fields — each is a single-value enum string on
# the chroma row (or ``""`` when not assessed). Listed here so the
# flattener / read paths agree on which fields exist.
_QUALITY_CATEGORICAL_FIELDS: tuple[str, ...] = (
    "quality",
    "complexity",
    "security",
    "testing",
    "testability",
    "documentation",
    "performance",
    "issues",
    "maintainability",
    "architecture",
    "technical_debt",
    "cohesion",
    "coupling",
    "stability",
    "priority",
)

# Multi-value list fields — stored as ``\x1f``-bracketed strings.
_LIST_FIELDS: tuple[str, ...] = (
    "vulnerabilities",
    "frameworks",
    "domain",
    "concerns",
    "layers",
    "patterns",
    "keywords",
    "file_issues",
)


class CommitNotFoundError(Exception):
    """Raised when a commit's chroma directory doesn't exist."""

    def __init__(self, sha: str):
        super().__init__(f"No chroma index found for commit {sha}")
        self.sha = sha


class _BestChunkHit(BaseModel):
    """Best-scoring chunk for one parent document during semantic search.

    Internal scratch model — kept here rather than in ``schema/items.py``
    because it never crosses the public API.
    """

    score: float
    chunk_text: str
    chunk_id: str


class CodeIndex:
    """Per-project, per-commit code index."""

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
        self.manifest = Manifest(project=project, data_dir=data_dir)
        # Per-(commit_sha) ChromaDB clients; opened lazily, reused.
        self._clients: dict[str, Any] = {}
        self._file_refs: Any | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Drop all cached chromadb clients. Persistent data stays on disk."""
        async with self._lock:
            self._clients.clear()

    def has_commit(self, sha: str) -> bool:
        """Return True iff a chroma directory exists on disk for ``sha``."""
        if not sha:
            return False
        return commit_chroma_path(self.project, sha, data_dir=self.data_dir).exists()

    # -- Commit lifecycle ------------------------------------------------------

    async def prepare_commit(
        self,
        sha: str,
        *,
        parent_sha: str | None = None,
    ) -> Path:
        """Ensure ``<sha>.chroma/`` exists; copy from ``parent_sha`` if provided."""
        target = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        if target.exists():
            self.manifest.touch(sha)
            return target

        target.parent.mkdir(parents=True, exist_ok=True)
        if parent_sha:
            parent = commit_chroma_path(self.project, parent_sha, data_dir=self.data_dir)
            if parent.exists():
                await asyncio.to_thread(shutil.copytree, str(parent), str(target))
            else:
                logger.warning(
                    "parent commit %s missing; creating empty chroma for %s",
                    parent_sha,
                    sha,
                )
                target.mkdir()
        else:
            target.mkdir()
        self.manifest.upsert_commit(sha)
        return target

    async def apply_delta(self, jsonl_path: str | Path):
        """Apply a producer-emitted JSONL changeset to this project."""
        from ember_code.core.code_index.delta import apply_delta

        return await apply_delta(
            index=self,
            file_refs=self._file_reference_service(),
            jsonl_path=jsonl_path,
        )

    def _file_reference_service(self):
        """Lazily build a ``FileReferenceService`` against the per-project SQLite."""
        if self._file_refs is None:
            from ember_code.core.code_index.paths import state_db_path
            from ember_code.core.code_index.pg.file_reference import FileReferenceService
            from ember_code.core.db.database import Database

            db = Database(state_db_path(self.project, data_dir=self.data_dir))
            self._file_refs = FileReferenceService(db)
        return self._file_refs

    async def set_head(self, sha: str) -> None:
        self.manifest.set_head(sha)

    def head(self) -> str | None:
        return self.manifest.load().head

    # -- Indexing --------------------------------------------------------------

    async def add_item(self, sha: str, item: CodeIndexItem) -> None:
        """Insert/replace an item + its chunks in ``<sha>.chroma/``."""
        await self.prepare_commit(sha)
        docs, chunks = await self._collections(sha)

        document_text = item.content or ""
        doc_metadata = _flatten_item_metadata(item)
        await asyncio.to_thread(
            docs.upsert,
            ids=[item.item_id],
            documents=[document_text],
            metadatas=[doc_metadata],
        )

        # Replace the chunk set for this item.
        await asyncio.to_thread(chunks.delete, where={"parent_doc_id": item.item_id})
        chunk_texts = self._chunk_text(document_text)
        if chunk_texts:
            chunk_ids = [f"{item.item_id}::{i}" for i in range(len(chunk_texts))]
            chunk_metadatas = [
                {
                    "parent_doc_id": item.item_id,
                    "chunk_index": i,
                    "name": item.name or "",
                    "type": item.type.value if hasattr(item.type, "value") else str(item.type),
                    "kind": item.kind or "",
                    "path": item.path or "",
                    "file_extension": item.file_extension or "",
                    "repository_id": item.repository_id or "",
                }
                for i in range(len(chunk_texts))
            ]
            await asyncio.to_thread(
                chunks.upsert,
                ids=chunk_ids,
                documents=chunk_texts,
                metadatas=chunk_metadatas,
            )
        self.manifest.touch(sha)

    async def remove_item(self, sha: str, item_id: str) -> None:
        """Drop an item and all its chunks from ``<sha>.chroma/``."""
        if not commit_chroma_path(self.project, sha, data_dir=self.data_dir).exists():
            return
        docs, chunks = await self._collections(sha)
        await asyncio.to_thread(docs.delete, ids=[item_id])
        await asyncio.to_thread(chunks.delete, where={"parent_doc_id": item_id})
        self.manifest.touch(sha)

    # -- Reads -----------------------------------------------------------------

    async def search(
        self,
        *,
        query: str,
        limit: int = 20,
        commit: str | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[CodeIndexResult]:
        """Semantic search inside one commit's index.

        ``where`` is the chroma metadata filter applied against the
        chunks collection — the codeindex_query tool builds it from
        its structured args; callers shouldn't construct it by hand.
        """
        sha = commit or self.head()
        if sha is None:
            return []
        chroma_dir = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        if not chroma_dir.exists():
            return []

        docs, chunks = await self._collections(sha)
        if await asyncio.to_thread(chunks.count) == 0:
            return []
        n = max(limit * 4, limit)

        # Quality / categorical fields live on parent doc metadata, not
        # on chunks. So when a ``where`` filter is supplied, resolve it
        # against the parents collection first to get matching IDs,
        # then narrow the chunk query to ``parent_doc_id $in <ids>``.
        chunk_where: dict[str, Any] | None = None
        if where:
            parent_ids = await self._resolve_parent_ids_for(docs, where)
            if not parent_ids:
                return []
            chunk_where = {"parent_doc_id": {"$in": parent_ids}}

        query_kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": n,
            "include": ["documents", "metadatas", "distances"],
        }
        if chunk_where is not None:
            query_kwargs["where"] = chunk_where
        result = await asyncio.to_thread(chunks.query, **query_kwargs)
        ids = (result.get("ids") or [[]])[0]
        chunk_docs = (result.get("documents") or [[]])[0]
        chunk_metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        if not ids:
            return []

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

        if not best:
            return []

        parent_ids = list(best.keys())
        parents = await asyncio.to_thread(
            docs.get, ids=parent_ids, include=["documents", "metadatas"]
        )
        parent_rows = {
            pid: (text or "", meta or {})
            for pid, text, meta in zip(
                parents.get("ids", []) or [],
                parents.get("documents", []) or [],
                parents.get("metadatas", []) or [],
                strict=False,
            )
        }

        out: list[CodeIndexResult] = []
        for parent_id, hit in best.items():
            content_text, parent_meta = parent_rows.get(parent_id, ("", {}))
            preview = hit.chunk_text
            truncated = preview[:1000] + "..." if len(preview) > 1000 else preview
            out.append(
                _meta_to_result(
                    parent_id,
                    parent_meta,
                    sha,
                    content=content_text,
                    score=hit.score,
                    chunk_preview=truncated,
                )
            )
        out.sort(key=lambda r: r.score or 0.0, reverse=True)
        self.manifest.touch(sha)
        return out[:limit]

    async def search_among(
        self,
        *,
        query: str,
        candidate_ids: list[str],
        limit: int,
        commit: str | None = None,
    ) -> list[CodeIndexResult]:
        """Like :meth:`search` but restricted to a fixed set of parent doc IDs.

        Used by the disambiguation-refs path on ``codeindex_query``: given
        the reference graph of an item (its callers / callees), this scores
        each reference's similarity to the original ``query_text`` and
        returns the top-K with full content. The restriction is applied
        chunk-side via ``where={"parent_doc_id": {"$in": candidate_ids}}``
        — the same mechanism :meth:`search` uses for typed-filter queries.

        Returns ``[]`` on any of:
          - no head commit
          - no chroma dir for the commit
          - empty ``candidate_ids``
          - empty chunks collection
          - chroma returned no matches

        The chunk→doc dedup, score normalization, and ``CodeIndexResult``
        construction match :meth:`search`. Only the where-filter source
        differs.
        """
        sha = commit or self.head()
        if sha is None or not candidate_ids:
            return []
        chroma_dir = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        if not chroma_dir.exists():
            return []
        docs, chunks = await self._collections(sha)
        if await asyncio.to_thread(chunks.count) == 0:
            return []

        chunk_where = {"parent_doc_id": {"$in": list(candidate_ids)}}
        n = max(limit * 4, limit)
        result = await asyncio.to_thread(
            chunks.query,
            query_texts=[query],
            n_results=n,
            include=["documents", "metadatas", "distances"],
            where=chunk_where,
        )
        ids = (result.get("ids") or [[]])[0]
        chunk_docs = (result.get("documents") or [[]])[0]
        chunk_metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        if not ids:
            return []

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

        if not best:
            return []

        parent_ids = list(best.keys())
        parents = await asyncio.to_thread(
            docs.get, ids=parent_ids, include=["documents", "metadatas"]
        )
        parent_rows = {
            pid: (text or "", meta or {})
            for pid, text, meta in zip(
                parents.get("ids", []) or [],
                parents.get("documents", []) or [],
                parents.get("metadatas", []) or [],
                strict=False,
            )
        }

        out: list[CodeIndexResult] = []
        for parent_id, hit in best.items():
            content_text, parent_meta = parent_rows.get(parent_id, ("", {}))
            preview = hit.chunk_text
            truncated = preview[:1000] + "..." if len(preview) > 1000 else preview
            out.append(
                _meta_to_result(
                    parent_id,
                    parent_meta,
                    sha,
                    content=content_text,
                    score=hit.score,
                    chunk_preview=truncated,
                )
            )
        out.sort(key=lambda r: r.score or 0.0, reverse=True)
        self.manifest.touch(sha)
        return out[:limit]

    async def filter_items(
        self,
        *,
        where: dict[str, Any] | None = None,
        ids: list[str] | None = None,
        limit: int = 20,
        commit: str | None = None,
    ) -> list[CodeIndexResult]:
        """Direct fetch / filter against the documents collection (no semantic search)."""
        sha = commit or self.head()
        if sha is None:
            return []
        chroma_dir = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        if not chroma_dir.exists():
            return []

        docs, _ = await self._collections(sha)
        get_kwargs: dict[str, Any] = {
            "include": ["documents", "metadatas"],
            "limit": limit,
        }
        if ids:
            get_kwargs["ids"] = ids
        if where:
            get_kwargs["where"] = where

        page = await asyncio.to_thread(docs.get, **get_kwargs)
        item_ids = page.get("ids") or []
        documents = page.get("documents") or []
        metadatas = page.get("metadatas") or []

        out: list[CodeIndexResult] = []
        for item_id, doc_text, meta in zip(item_ids, documents, metadatas, strict=False):
            out.append(_meta_to_result(item_id, meta or {}, sha, content=doc_text or ""))
        self.manifest.touch(sha)
        return out

    async def get_item(
        self,
        item_id: str,
        *,
        commit: str | None = None,
    ) -> CodeIndexResult | None:
        sha = commit or self.head()
        if sha is None:
            return None
        chroma_dir = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        if not chroma_dir.exists():
            return None
        docs, _ = await self._collections(sha)
        page = await asyncio.to_thread(docs.get, ids=[item_id], include=["documents", "metadatas"])
        ids = page.get("ids") or []
        if not ids:
            return None
        text = (page.get("documents") or [""])[0]
        meta = (page.get("metadatas") or [{}])[0]
        self.manifest.touch(sha)
        return _meta_to_result(ids[0], meta, sha, content=text)

    # -- Retention -------------------------------------------------------------

    async def clean(
        self,
        *,
        keep_recent_days: int = 30,
    ) -> list[str]:
        """Drop commits not on a branch and idle longer than ``keep_recent_days``.

        Selective housekeeping — preserves HEAD and every commit
        pointed to by a local branch. For a full wipe-and-resync,
        the chroma dir under ``~/.ember/projects/<id>/code_index``
        can be removed manually; there is no in-process verb for
        that yet.
        """
        # Refresh branch_refs from git so retention has fresh data.
        branch_map = _branch_heads(self.project)
        per_commit_branches: dict[str, list[str]] = {}
        for branch, sha in branch_map.items():
            per_commit_branches.setdefault(sha, []).append(branch)
        self.manifest.update_branch_refs(per_commit_branches)

        state = self.manifest.load()
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_recent_days)
        to_drop: list[str] = []
        for sha, info in state.commits.items():
            if sha == state.head:
                continue
            if info.branch_refs:
                continue
            try:
                last_used = datetime.fromisoformat(info.last_used_at)
            except ValueError:
                last_used = datetime.now(timezone.utc)
            if last_used < cutoff:
                to_drop.append(sha)

        for sha in to_drop:
            chroma_dir = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
            if chroma_dir.exists():
                await asyncio.to_thread(shutil.rmtree, str(chroma_dir))
            self._clients.pop(sha, None)
            self.manifest.remove_commit(sha)
        return to_drop

    # -- Internal --------------------------------------------------------------

    async def _collections(self, sha: str) -> tuple[Any, Any]:
        client = await self._client_for(sha)
        docs = await asyncio.to_thread(_get_or_create_collection, client, DOCUMENTS_COLLECTION)
        chunks = await asyncio.to_thread(_get_or_create_collection, client, CHUNKS_COLLECTION)
        return docs, chunks

    @staticmethod
    async def _resolve_parent_ids_for(docs: Any, where: dict[str, Any]) -> list[str]:
        """Find parent doc IDs matching a metadata ``where`` filter.

        Used to translate a quality-field filter (which lives on parent
        docs) into a chunk-side filter (which can only match the
        denormalized columns on chunk metadata). Capped at 10k IDs —
        beyond that the index user really wants ``filter_items``, not
        a semantic search inside an enormous candidate set.
        """
        page = await asyncio.to_thread(
            docs.get,
            where=where,
            limit=10_000,
            include=[],
        )
        return list(page.get("ids") or [])

    async def _client_for(self, sha: str) -> Any:
        if sha in self._clients:
            return self._clients[sha]
        path = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        path.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            if sha not in self._clients:
                self._clients[sha] = await asyncio.to_thread(_open_client, path)
            return self._clients[sha]

    def _chunk_text(self, content: str) -> list[str]:
        if not content:
            return []
        chunks = self.chunker.chunk(Document(content=content))
        return [c.content for c in chunks if c.content]


# -- Helpers ------------------------------------------------------------------


def _open_client(path: Path) -> Any:
    import chromadb

    return chromadb.PersistentClient(path=str(path))


def _get_or_create_collection(client: Any, name: str) -> Any:
    """Get or create a chroma collection with high-recall HNSW config.

    Chroma defaults to ``hnsw:search_ef=10`` which gives broken recall
    on top-K queries with K > ~10 (the index returns near-floor matches
    instead of the truly closest neighbors). At our scale (a few tens
    of thousands of chunks) HNSW with high search_ef is effectively
    exact, latency-cheap, and matches the precision the agent expects.

    These parameters are baked into the collection's metadata at
    creation time. Existing collections created without them keep
    chroma's defaults until rebuilt — see ``scripts/reindex_hnsw.py``.

    HNSW knobs (chroma supports the ``hnsw:*`` metadata keys):
      - ``hnsw:space=cosine`` — explicit; the score-as-1-minus-distance
        formula assumes cosine.
      - ``hnsw:M=32`` — graph connectivity. Higher M = better graph
        topology = better recall at any search_ef. 32 is the sweet spot
        for most embedding sizes; doubles memory but our scale is tiny.
      - ``hnsw:construction_ef=400`` — graph build quality. Higher =
        better graph at index time, paid once.
      - ``hnsw:search_ef=1000`` — per-query candidate pool size. Higher
        = better recall at query time, paid every query. At 1000 with
        ~42k chunks, recall on top-K is ~100% for K up to ~100.
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


def _branch_heads(project: str | Path) -> dict[str, str]:
    """Return ``{branch_name: head_sha}`` for every local branch.

    Empty dict when the project isn't a git repo.
    """
    try:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
            capture_output=True,
            text=True,
            cwd=str(project),
            timeout=5,
        )
    except Exception as exc:
        logger.debug("git for-each-ref failed: %s", exc)
        return {}
    if result.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            branch, sha = parts
            out[branch] = sha
    return out


def _flatten_item_metadata(item: CodeIndexItem) -> dict[str, Any]:
    """Pack a :class:`CodeIndexItem`'s fields into chromadb-friendly metadata.

    Single-value fields land as exact-match strings; quality
    categoricals use ``""`` as the "not assessed" sentinel since
    chroma metadata can't hold ``None``. Multi-value lists use
    ``\\x1f`` brackets so ``$contains`` is exact-on-value.

    Line numbers use ``-1`` for "not applicable" (folder/file rows).
    """
    out: dict[str, Any] = {
        "name": item.name or "",
        "type": item.type.value if hasattr(item.type, "value") else str(item.type),
        "kind": item.kind or "",
        "entity_type": item.entity_type or "",
        "parent_id": item.parent_id or "",
        "file_extension": item.file_extension or "",
        "repository_id": item.repository_id or "",
        "path": item.path or "",
        "archived": bool(getattr(item, "archived", False)),
        "timestamp": item.timestamp or "",
        "token_count": int(item.token_count or 0),
        "line_from": int(item.line_from) if item.line_from is not None else -1,
        "line_to": int(item.line_to) if item.line_to is not None else -1,
        "needs_refactoring": bool(item.needs_refactoring)
        if item.needs_refactoring is not None
        else False,
    }
    for field in _QUALITY_CATEGORICAL_FIELDS:
        out[field] = getattr(item, field) or ""
    for field in _LIST_FIELDS:
        values = getattr(item, field) or []
        out[field] = _encode_bracketed_list(values)
    return out


def _encode_bracketed_list(values: Iterable[str]) -> str:
    """``["a", "b"]`` → ``"\x1fa\x1fb\x1f"`` for $contains exact match.

    Empty list → ``""`` (so ``$contains`` against any value misses cleanly).
    """
    parts = [str(v) for v in values if v]
    if not parts:
        return ""
    return _LIST_SEP + _LIST_SEP.join(parts) + _LIST_SEP


def _decode_bracketed_list(encoded: Any) -> list[str]:
    if not encoded:
        return []
    text = str(encoded)
    return [part for part in text.split(_LIST_SEP) if part]


def _line_or_none(value: Any) -> int | None:
    """Decode the chroma sentinel for "no line range".

    ``-1`` (or any negative value) means "not applicable"; convert
    back to ``None`` so consumers see a clean nullable.
    """
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _meta_to_result(
    item_id: str,
    meta: dict[str, Any],
    sha: str,
    *,
    content: str = "",
    score: float | None = None,
    chunk_preview: str | None = None,
) -> CodeIndexResult:
    """Build a :class:`CodeIndexResult` from one chroma row.

    Centralized so ``search`` / ``filter_items`` / ``get_item`` all
    return the same pydantic shape.
    """
    payload: dict[str, Any] = {
        "item_id": item_id,
        "name": meta.get("name", ""),
        "type": meta.get("type", ""),
        "kind": meta.get("kind", ""),
        "entity_type": meta.get("entity_type", ""),
        "path": meta.get("path", ""),
        "file_extension": meta.get("file_extension", ""),
        "repository_id": meta.get("repository_id", ""),
        "parent_id": meta.get("parent_id", ""),
        "archived": bool(meta.get("archived", False)),
        "timestamp": meta.get("timestamp", ""),
        "token_count": int(meta.get("token_count", 0) or 0),
        "line_from": _line_or_none(meta.get("line_from")),
        "line_to": _line_or_none(meta.get("line_to")),
        "needs_refactoring": bool(meta.get("needs_refactoring", False)),
        "commit": sha,
        "content": content,
        "score": score,
        "chunk_preview": chunk_preview,
    }
    for field in _QUALITY_CATEGORICAL_FIELDS:
        payload[field] = meta.get(field, "")
    for field in _LIST_FIELDS:
        payload[field] = _decode_bracketed_list(meta.get(field))
    return CodeIndexResult.model_validate(payload)
