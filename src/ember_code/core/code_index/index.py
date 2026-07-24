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
- :meth:`clean` — drop commits not on a branch and idle > N days.

Quality / category metadata are first-class typed chroma fields — each
quality dimension is its own indexed string column, each multi-value
category is its own ``\\x1f``-bracketed string. There is no ``tags``
field; the ``codeindex_query`` tool builds typed where-clauses from
its enum args without any string-tag parsing.

The class is a thin orchestrator over four collaborators:

  - :class:`ChromaRowCodec` — the item ↔ row-metadata wire codec.
  - :class:`ChromaClientFactory` — client + collection lifecycle.
  - :class:`GitBranchReader` — local branch resolution for retention.
  - :class:`ChunkSearch` — the semantic search engine.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agno.knowledge.chunking.recursive import RecursiveChunking
from agno.knowledge.chunking.strategy import ChunkingStrategy
from agno.knowledge.document.base import Document

from ember_code.core.code_index.chroma_client_factory import (
    CHUNKS_COLLECTION,
    DOCUMENTS_COLLECTION,
    ChromaClientFactory,
)
from ember_code.core.code_index.chroma_codec import ChromaRowCodec
from ember_code.core.code_index.chunk_search import ChunkSearch
from ember_code.core.code_index.db.file_reference import FileReferenceService
from ember_code.core.code_index.delta import apply_delta
from ember_code.core.code_index.git_branches import GitBranchReader
from ember_code.core.code_index.manifest import Manifest
from ember_code.core.code_index.paths import (
    code_index_dir,
    commit_chroma_path,
    state_db_path,
)
from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.code_index.schema.chroma_row import ChromaGetPage
from ember_code.core.code_index.schema.items import CodeIndexItem, CodeIndexResult
from ember_code.core.code_index.schema.stats import HeadStats
from ember_code.core.code_index.schema.where_filter import ChromaWhereFilter
from ember_code.core.db.database import Database

logger = logging.getLogger(__name__)


class CommitNotFoundError(Exception):
    """Raised when a commit's chroma directory doesn't exist."""

    def __init__(self, sha: str):
        super().__init__(f"No chroma index found for commit {sha}")
        self.sha = sha


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

        # Collaborators — composed once, reused across every call.
        self._codec = ChromaRowCodec()
        self._client_factory = ChromaClientFactory()
        self._branches = GitBranchReader()
        self._chunk_search = ChunkSearch(codec=self._codec)

    async def close(self) -> None:
        """Drop all cached chromadb clients. Persistent data stays on disk."""
        async with self._lock:
            self._clients.clear()

    def has_commit(self, sha: str) -> bool:
        """Return True iff the commit is fully indexed locally.

        Both the chroma dir AND a manifest entry must be present. The
        manifest check lets ``forget_commit`` mark a commit as
        un-indexed without ``rmtree``-ing under chromadb's live
        client (see ``forget_commit`` for why we avoid that).
        """
        if not sha:
            return False
        if not commit_chroma_path(self.project, sha, data_dir=self.data_dir).exists():
            return False
        return sha in self.manifest.load().commits

    async def forget_commit(self, sha: str) -> bool:
        """Wipe a commit's local state so the next sync rebuilds from scratch.

        Used by ``/codeindex resync`` when the local index has drifted
        from the cloud definition.

        We deliberately don't ``rmtree`` the ``<sha>.chroma/`` directory —
        the two-phase teardown is documented on
        :meth:`ChromaClientFactory.drop_commit_collections`. The manifest
        entry is dropped so ``has_commit`` reports the commit as missing.
        """
        if not sha:
            return False
        target = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        had_state = target.exists()

        if had_state:
            try:
                client = await self._client_for(sha)
                await asyncio.to_thread(self._client_factory.drop_commit_collections, client)
            except Exception as exc:
                logger.debug("forget_commit: chroma teardown failed (%s)", exc)

        try:
            self.manifest.remove_commit(sha)
        except Exception:
            logger.debug("manifest had no record of %s", sha)

        return had_state

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
        return await apply_delta(
            index=self,
            file_refs=self.file_reference_service(),
            jsonl_path=jsonl_path,
        )

    def file_reference_service(self):
        """Lazily build a ``FileReferenceService`` against the per-project SQLite.

        Public accessor — the previous ``_file_reference_service`` name
        implied private state, but the sync manager and the codeindex
        tools (:mod:`disambiguation`, :mod:`tree_service`) all need to
        share the same service. The leading underscore was a Rule-6
        reach-in target; renaming it seals that hole.
        """
        if self._file_refs is None:
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
        doc_metadata = self._codec.flatten(item).to_chroma_dict()
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
                self._codec.flatten_chunk_row(item, i).to_chroma_dict()
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
        pair = await self._open_or_none(sha)
        if pair is None:
            return
        docs, chunks = pair
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
        where: ChromaWhereFilter | dict[str, Any] | None = None,
    ) -> list[CodeIndexResult]:
        """Semantic search inside one commit's index.

        ``where`` is a :class:`ChromaWhereFilter` (or a raw chroma dict
        for legacy callers) applied against the chunks collection —
        the codeindex_query tool builds it from its structured args;
        callers shouldn't construct it by hand.
        """
        sha = commit or self.head()
        if sha is None:
            return []
        pair = await self._open_or_none(sha)
        if pair is None:
            return []
        docs, chunks = pair

        # Quality / categorical fields live on parent doc metadata, not
        # on chunks. So when a ``where`` filter is supplied, resolve it
        # against the parents collection first to get matching IDs,
        # then narrow the chunk query to ``parent_doc_id $in <ids>``.
        chunk_where = await self._resolve_chunk_where(docs, where)
        if chunk_where is _NARROWED_TO_EMPTY:
            return []

        results = await self._chunk_search.execute(
            docs=docs,
            chunks=chunks,
            sha=sha,
            query=query,
            chunk_where=chunk_where,
            limit=limit,
        )
        if results:
            self.manifest.touch(sha)
        return results

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
        """
        sha = commit or self.head()
        if sha is None or not candidate_ids:
            return []
        pair = await self._open_or_none(sha)
        if pair is None:
            return []
        docs, chunks = pair

        chunk_where = {"parent_doc_id": {"$in": list(candidate_ids)}}
        results = await self._chunk_search.execute(
            docs=docs,
            chunks=chunks,
            sha=sha,
            query=query,
            chunk_where=chunk_where,
            limit=limit,
        )
        if results:
            self.manifest.touch(sha)
        return results

    async def filter_items(
        self,
        *,
        where: ChromaWhereFilter | dict[str, Any] | None = None,
        ids: list[str] | None = None,
        limit: int = 20,
        commit: str | None = None,
    ) -> list[CodeIndexResult]:
        """Direct fetch / filter against the documents collection (no semantic search)."""
        sha = commit or self.head()
        if sha is None:
            return []
        pair = await self._open_or_none(sha)
        if pair is None:
            return []
        docs, _ = pair

        rendered_where = self._render_where(where)
        get_kwargs: dict[str, Any] = {
            "include": ["documents", "metadatas"],
            "limit": limit,
        }
        if ids:
            get_kwargs["ids"] = ids
        if rendered_where is not None:
            get_kwargs["where"] = rendered_where

        raw = await asyncio.to_thread(docs.get, **get_kwargs)
        page = ChromaGetPage.from_chroma(raw)

        out: list[CodeIndexResult] = []
        for item_id, doc_text, meta in zip(page.ids, page.documents, page.metadatas, strict=False):
            out.append(self._codec.parse(item_id, meta or {}, sha, content=doc_text or ""))
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
        pair = await self._open_or_none(sha)
        if pair is None:
            return None
        docs, _ = pair
        raw = await asyncio.to_thread(docs.get, ids=[item_id], include=["documents", "metadatas"])
        page = ChromaGetPage.from_chroma(raw)
        if not page.ids:
            return None
        text = page.documents[0] if page.documents else ""
        meta = page.metadatas[0] if page.metadatas else {}
        self.manifest.touch(sha)
        return self._codec.parse(page.ids[0], meta or {}, sha, content=text or "")

    # -- Retention -------------------------------------------------------------

    async def clean(
        self,
        *,
        keep_recent_days: int = 30,
    ) -> list[str]:
        """Drop commits not on a branch and idle longer than ``keep_recent_days``.

        Selective housekeeping — preserves HEAD and every commit
        pointed to by a local branch.

        The eviction is **two-phase**: this call empties the chroma
        data via chromadb's own ``delete_collection`` API (encapsulated
        on :class:`ChromaClientFactory`) and drops the manifest entry,
        but leaves the (now-empty) directory on disk.
        :meth:`sweep_stale_dirs` reclaims the husk at session startup,
        before any client has been opened in this process.
        """
        # Refresh branch_refs from git so retention has fresh data.
        branch_map = self._branches.load(self.project)
        self.manifest.update_branch_refs(branch_map.per_commit())

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
                # Empty the collections through chromadb's API. The
                # directory stays — startup sweep reclaims it.
                try:
                    client = await self._client_for(sha)
                    await asyncio.to_thread(self._client_factory.drop_commit_collections, client)
                except Exception as exc:
                    logger.debug("clean: chroma teardown failed for %s (%s)", sha[:8], exc)
            self.manifest.remove_commit(sha)
        return to_drop

    def sweep_stale_dirs(self) -> list[str]:
        """Reclaim chroma directories that aren't tracked in the manifest.

        :meth:`clean` and :meth:`forget_commit` both drop manifest
        entries without ``rmtree``-ing — see those methods for why
        rmtree under a live chromadb client is unsafe. This sweep
        closes the loop by removing those orphaned directories from
        disk. It is only safe to call **before** any
        :meth:`_client_for` call in this process, since otherwise
        chromadb's process-level cache might still hold open
        handles to the path. Typical placement: at session startup,
        before the initial ``sync_now``.
        """
        base = code_index_dir(self.project, data_dir=self.data_dir)
        if not base.is_dir():
            return []
        tracked = set(self.manifest.load().commits.keys())
        removed: list[str] = []
        for child in base.iterdir():
            if not child.is_dir() or not child.name.endswith(".chroma"):
                continue
            sha = child.name[: -len(".chroma")]
            if sha in tracked:
                continue
            try:
                shutil.rmtree(str(child), ignore_errors=True)
                removed.append(sha)
            except OSError as exc:
                logger.debug("sweep_stale_dirs: failed to remove %s (%s)", child, exc)
        return removed

    async def head_stats(self, sha: str) -> HeadStats:
        """Quick per-commit stats for the CodeIndex panel.

        The index stores items at three granularities (``folder`` /
        ``file`` / ``entity``), so a naive ``docs.count()`` would
        conflate files with the functions and classes inside them —
        producing Coverage values above 100%. We filter to
        ``type == "file"`` and dedupe by path so the numbers are
        directly comparable to ``git ls-files``.
        """
        pair = await self._open_or_none(sha)
        if pair is None:
            return HeadStats(files_indexed=0, languages_indexed={})
        docs, _ = pair
        total = await asyncio.to_thread(docs.count)
        if total == 0:
            return HeadStats(files_indexed=0, languages_indexed={})
        # Fetch only file-typed docs' metadatas — folders/entities
        # are noise for file-count purposes. ``where`` filters at the
        # chroma layer so we don't pull entity rows over the wire on
        # large repos.
        raw = await asyncio.to_thread(
            docs.get,
            where={"type": "file"},
            include=["metadatas"],
            limit=50_000,
        )
        page = ChromaGetPage.from_chroma(raw)
        seen_paths: set[str] = set()
        ext_counts: Counter[str] = Counter()
        for meta in page.metadatas:
            m = meta or {}
            path = (m.get("path") or "").strip()
            if path and path in seen_paths:
                continue
            if path:
                seen_paths.add(path)
            ext = (m.get("file_extension") or "").lower()
            ext_counts[ext or "(other)"] += 1
        return HeadStats(
            files_indexed=len(seen_paths) if seen_paths else sum(ext_counts.values()),
            languages_indexed=dict(ext_counts),
        )

    # -- Internal --------------------------------------------------------------

    async def _open_or_none(self, sha: str) -> tuple[Any, Any] | None:
        """Return ``(docs, chunks)`` for ``sha`` or ``None`` if the commit
        isn't materialized.

        Consolidates the "no chroma dir → return empty" guard that used
        to appear in every read path.
        """
        chroma_dir = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        if not chroma_dir.exists():
            return None
        return await self._collections(sha)

    async def _collections(self, sha: str) -> tuple[Any, Any]:
        client = await self._client_for(sha)
        docs, chunks = await asyncio.to_thread(self._client_factory.docs_and_chunks, client)
        return docs, chunks

    async def _client_for(self, sha: str) -> Any:
        """Open (or return cached) chromadb client for ``sha``.

        The per-sha cache lives here on :class:`CodeIndex`, NOT on the
        factory — chromadb has its own process-level path cache, and
        double-caching would break the two-phase teardown workaround.
        """
        if sha in self._clients:
            return self._clients[sha]
        path = commit_chroma_path(self.project, sha, data_dir=self.data_dir)
        path.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            if sha not in self._clients:
                self._clients[sha] = await asyncio.to_thread(self._client_factory.open, path)
            return self._clients[sha]

    async def _resolve_chunk_where(
        self,
        docs: Any,
        where: ChromaWhereFilter | dict[str, Any] | None,
    ) -> dict[str, Any] | None | object:
        """Translate a parent-side ``where`` into a chunk-side ``where``.

        Quality / categorical filters live on parent doc metadata, not
        on chunks. So we resolve the parent IDs first, then rewrite the
        chunk-side filter to ``parent_doc_id $in <ids>``. Returns
        :data:`_NARROWED_TO_EMPTY` when the parent filter matches
        nothing — callers short-circuit to an empty result.
        """
        rendered = self._render_where(where)
        if not rendered:
            return None
        parent_ids = await self._chunk_search.resolve_parent_ids(docs, rendered)
        if not parent_ids:
            return _NARROWED_TO_EMPTY
        return {"parent_doc_id": {"$in": parent_ids}}

    @staticmethod
    def _render_where(
        where: ChromaWhereFilter | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Coerce a filter argument to the raw chroma ``where`` dict.

        Accepts both the typed :class:`ChromaWhereFilter` and — for
        backwards compat with tests/callers that still pass a hand-built
        dict — a raw ``dict``. Returns ``None`` when the filter is empty.
        """
        if where is None:
            return None
        if isinstance(where, ChromaWhereFilter):
            return where.to_chroma_where()
        if isinstance(where, dict):
            return where or None
        raise TypeError(f"unsupported where filter type: {type(where)!r}")

    def _chunk_text(self, content: str) -> list[str]:
        if not content:
            return []
        chunks = self.chunker.chunk(Document(content=content))
        return [c.content for c in chunks if c.content]


# Sentinel returned by :meth:`CodeIndex._resolve_chunk_where` when the
# parent filter narrowed to zero IDs — the read path short-circuits to
# an empty result without touching chunks.
_NARROWED_TO_EMPTY: object = object()


__all__ = [
    "CHUNKS_COLLECTION",
    "DOCUMENTS_COLLECTION",
    "ChromaWhereFilter",
    "CodeIndex",
    "CommitNotFoundError",
    "HeadStats",
]
