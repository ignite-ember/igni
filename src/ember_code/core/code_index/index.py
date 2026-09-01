"""Per-project, per-commit code index backed by Neo4j.

Each commit's data lives in its own Neo4j process (one driver
per (project, commit) pair, refcounted via
:class:`Neo4jRuntime`). Item / chunk / edge data lands in
``<Item>`` / ``<Chunk>`` / ``<REL>`` nodes plus a 384-dim vector
index on :Chunk.

Lifecycle:

- :meth:`prepare_commit` — copy parent → child (or create empty), update manifest.
- :meth:`apply_delta` — apply a JSONL of file-level changes.
- :meth:`set_head` — point the manifest's ``head`` at a commit.
- :meth:`search` / :meth:`get_item` — query a commit (defaults to head).
- :meth:`clean` — drop commits not on a branch and idle > N days.

Quality / category metadata are first-class typed :Item
properties; the ``codeindex_query`` tool builds typed
where-clauses from its enum args without any string-tag
parsing.

The class is a thin orchestrator over:

  - :class:`Embedder` — 384-dim vector producer (see
    :mod:`core.code_index.embedder`).
  - :class:`Neo4jClient` — per-commit data access (items, chunks,
    edges, metadata). One instance per (project, commit) pair
    when ``runtime=`` is set; one shared instance when
    ``neo4j_client=`` is passed (test escape hatch).
  - :class:`GitBranchReader` — local branch resolution for
    retention.
  - :class:`Neo4jMetaClient` — per-project admin (head pointer,
    branch pins, commit tracking) — only used by ``clean`` /
    retention.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agno.knowledge.chunking.recursive import RecursiveChunking
from agno.knowledge.chunking.strategy import ChunkingStrategy
from agno.knowledge.document.base import Document

from ember_code.core.code_index.db.file_reference import FileReferenceService
from ember_code.core.code_index.delta import apply_delta
from ember_code.core.code_index.embedder import Embedder, HashEmbedder
from ember_code.core.code_index.git_branches import GitBranchReader
from ember_code.core.code_index.manifest import Manifest
from ember_code.core.code_index.paths import (
    code_index_dir,
    legacy_commit_index_path,
)
from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.code_index.schema.items import ChunkRow, CodeIndexItem, CodeIndexResult
from ember_code.core.code_index.schema.stats import HeadStats
from ember_code.core.code_index.schema.where_filter import ChromaWhereFilter
from ember_code.core.paths import DEFAULT_DATA_DIR

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
        data_dir: str | Path = DEFAULT_DATA_DIR,
        chunker: ChunkingStrategy | None = None,
        neo4j_client: Any | None = None,
        runtime: Any | None = None,
        embedder: Embedder | None = None,
    ):
        self.project = project
        self.project_id = resolve_project_id(project)
        self.data_dir = data_dir
        self.chunker = chunker or RecursiveChunking(chunk_size=800, overlap=100)
        self.manifest = Manifest(project=project, data_dir=data_dir)
        # Per-(commit_sha) Neo4j clients when ``runtime=`` is set;
        # one global client when ``neo4j_client=`` is passed.
        self._clients: dict[str, Any] = {}
        self._file_refs: Any | None = None
        # ``runtime=`` is the production seam: a real
        # :class:`Neo4jRuntime` spawns a process per (project, commit)
        # and hands back a driver for that commit. ``neo4j_client=``
        # is the test escape hatch (one client for every commit).
        # ``embedder`` supplies the 384-dim vectors for
        # ``add_item`` / ``search``; the default ``HashEmbedder`` is
        # deterministic and offline-safe for tests.
        self._neo4j_client = neo4j_client
        self._neo4j_runtime = runtime
        if embedder is None:
            # HashEmbedder is SHA-256 of the text split into 384 coordinates: it
            # separates *distinct* chunks and carries no meaning, so
            # ``db.index.vector.queryNodes`` can only match text that is
            # byte-identical. Defaulting to it silently is how an index ended up
            # with 1.5M embedded chunks that could not answer a single "find the
            # code that does X" query. Tests want it; production must not have it
            # by accident.
            logger.warning(
                "CodeIndex built with no embedder — falling back to HashEmbedder. "
                "Chunk embeddings will carry no meaning and semantic search will "
                "not work. Pass LiveEmbedder() for real vectors."
            )
        self._embedder: Embedder = embedder or HashEmbedder()
        self._lock = asyncio.Lock()
        self._branches = GitBranchReader()

    async def close(self) -> None:
        """Drop all cached neo4j clients. Persistent data stays on disk."""
        async with self._lock:
            self._clients.clear()

    def has_commit(self, sha: str) -> bool:
        """Return True iff the commit is fully indexed locally.

        On the neo4j path the per-commit process owns the data;
        we only check the manifest. The runtime is the source of
        truth for ``drop`` / ``start_for_commit`` — the manifest
        entry stays in sync with the runtime's refcount.
        """
        if not sha:
            return False
        return sha in self.manifest.load().commits

    async def forget_commit(self, sha: str) -> bool:
        """Wipe a commit's local state so the next sync rebuilds from scratch.

        Used by ``/codeindex resync`` when the local index has drifted
        from the cloud definition.

        On the neo4j path the per-commit data lives in its own
        process's DB; we ask the runtime to drop it. The
        per-commit state dir is left in place (informational —
        :meth:`sweep_stale_dirs` reclaims it on next startup). The
        manifest entry is dropped so ``has_commit`` reports the
        commit as missing.
        """
        if not sha:
            return False
        had_state = sha in self.manifest.load().commits
        if had_state and self._neo4j_runtime is not None:
            try:
                # Ask the runtime to stop this commit's process.
                # ``stop_for_commit`` is idempotent — safe even if
                # the process is already gone.
                await self._neo4j_runtime.stop_for_commit(self.project_id, sha)
            except Exception as exc:
                logger.debug("forget_commit: neo4j stop failed for %s (%s)", sha[:8], exc)
        elif had_state and self._neo4j_client is not None:
            try:
                # Shared-client test path: drop the per-commit data
                # via the static helper on Neo4jClient.
                from ember_code.core.code_index.neo4j_client import Neo4jClient

                await Neo4jClient.drop_database(self._neo4j_client._driver, self.project_id, sha)
            except Exception as exc:
                logger.debug("forget_commit: neo4j drop failed for %s (%s)", sha[:8], exc)
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
        """Ensure this commit's neo4j process is up + manifest entry exists.

        With the per-process isolation model, each commit's data
        lives in a separate Neo4j process; the directory this
        returns is the *process state dir* (kept as a chroma-era
        artifact for back-compat — nothing writes into it). The
        actual item/chunk writes go through ``neo4j_client``.
        ``parent_sha`` is recorded on the manifest's parent chain
        but no per-commit data is copied (the process is empty).
        """
        if self._neo4j_runtime is not None:
            await self._neo4j_runtime.start_for_commit(self.project_id, sha)
        self.manifest.upsert_commit(sha)
        return legacy_commit_index_path(self.project, sha, data_dir=self.data_dir)

    async def apply_delta(self, jsonl_path: str | Path):
        """Apply a producer-emitted JSONL changeset to this project.

        When constructed with ``runtime=`` (a :class:`Neo4jRuntime`),
        this method ensures the runtime has a process for the commit
        being indexed, derives a per-commit ``Neo4jClient``, and
        routes ``upsert_reference`` ops through it. The chroma-backed
        item/chunk storage is unchanged in this branch; only file
        references traverse the neo4j path.
        """
        # When a runtime is configured, ``_client_for_active_commit``
        # ensures the runtime has a process for the commit, returns
        # the per-commit client, and ``file_reference_service()``
        # uses it (per-commit cached). When no runtime, ``commit_sha``
        # is irrelevant for the file-refs backend.
        neo4j_client = await self._client_for_active_commit(jsonl_path)
        commit_sha = neo4j_client.commit_sha if neo4j_client is not None else None
        file_refs = self.file_reference_service(commit_sha=commit_sha)
        return await apply_delta(
            index=self,
            file_refs=file_refs,
            jsonl_path=jsonl_path,
            neo4j_client=neo4j_client,
        )

    async def _client_for_active_commit(self, jsonl_path: str | Path) -> Any | None:
        """Return a per-commit ``Neo4jClient`` when ``runtime=`` is set.

        Reads the first line of ``jsonl_path`` (the mandatory
        ``commit`` op) to learn the active ``commit_sha``, then asks
        the runtime to ensure a process for the
        ``(project_hash, commit_sha)`` pair and hands back a client
        bound to the resulting driver. Returns ``None`` when no
        runtime is configured (the caller falls back to whatever
        backend the indexer was constructed with).
        """
        if self._neo4j_runtime is None:
            return None
        # Peek the first non-blank line for the commit sha. The
        # applier will re-parse the file; we just need the sha.
        commit_sha = ""
        with open(jsonl_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                op = json.loads(line)
                if op.get("op") != "commit":
                    raise ValueError(
                        f"first op of {jsonl_path} must be 'commit', got {op.get('op')!r}"
                    )
                commit_sha = op.get("sha", "")
                break
        if not commit_sha:
            raise ValueError(f"empty or non-commit-leading delta: {jsonl_path}")
        # Ensure the runtime has spawned (or re-attached to) a
        # process for this (project, commit) pair. ``start_for_commit``
        # is idempotent — subsequent callers get the same driver.
        await self._neo4j_runtime.start_for_commit(self.project_id, commit_sha)
        driver = self._neo4j_runtime.driver_for(self.project_id, commit_sha)
        # Lazy import — the project may not have the neo4j driver
        # installed when this module is loaded.
        from ember_code.core.code_index.neo4j_client import Neo4jClient

        return Neo4jClient(driver, self.project_id, commit_sha)

    def file_reference_service(self, *, commit_sha: str | None = None):
        """Lazily build a ``FileReferenceService``.

        Routes to the neo4j backend when ``neo4j_client`` was
        injected at construction OR when ``runtime=`` is set (the
        latter derives a per-commit client from the runtime; the
        ``commit_sha`` arg is the discriminator for that case).
        The service class is backend-pluggable (see
        :class:`db.file_reference.FileReferenceService`), so the
        switch is transparent to callers — the only difference is
        where the data lives.

        When ``runtime=`` is set, results are cached per ``commit_sha``
        so repeated reads of the same commit's references don't
        re-construct a client.
        """
        # Runtime path: per-commit client from the runtime.
        if self._neo4j_runtime is not None:
            assert commit_sha is not None, (
                "file_reference_service() with runtime= requires commit_sha"
            )
            cache = getattr(self, "_file_refs_by_commit", None)
            if cache is None:
                cache = {}
                self._file_refs_by_commit = cache  # type: ignore[attr-defined]
            existing = cache.get(commit_sha)
            if existing is not None:
                return existing
            # Caller is expected to have called
            # ``_client_for_active_commit`` (or equivalent) so the
            # runtime already has a process for this commit.
            driver = self._neo4j_runtime.driver_for(self.project_id, commit_sha)
            from ember_code.core.code_index.neo4j_client import Neo4jClient

            client = Neo4jClient(driver, self.project_id, commit_sha)
            svc = FileReferenceService(client)
            cache[commit_sha] = svc
            return svc

        # Explicit client shortcut (no runtime).
        if self._neo4j_client is not None:
            if self._file_refs is None:
                self._file_refs = FileReferenceService(self._neo4j_client)
            return self._file_refs

        raise NotImplementedError(
            "file_reference_service() requires a neo4j backend — pass "
            "``runtime=`` (production) or ``neo4j_client=`` (test) to "
            "the constructor. The legacy SQLite path was removed when "
            "code_index migrated to neo4j."
        )

    async def set_head(self, sha: str) -> None:
        self.manifest.set_head(sha)

    def head(self) -> str | None:
        return self.manifest.load().head

    # -- Indexing --------------------------------------------------------------

    def embed_query(self, text: str) -> list[float]:
        """Embed one query string for a vector lookup.

        Exposed because the vector index takes 384 floats and an agent has a
        sentence: the tool layer passes text and binds the result as
        ``$query_vector``. Uses the same embedder the chunks were written with,
        which is the only way the comparison means anything — a graph built with
        ``HashEmbedder`` and queried with a real model would return noise while
        looking like it worked.
        """
        return self._embedder.embed([text])[0]

    async def client_for(self, sha: str | None = None) -> Any | None:
        """Public accessor for the per-commit ``Neo4jClient``.

        Args:
            sha: explicit commit SHA. Defaults to the head commit
                (``self.head()``).

        Returns: the :class:`Neo4jClient` for that commit, or
        ``None`` when no runtime / injected client is configured
        (the test escape hatch of ``chroma``-backed runs).

        Used by ``codeindex_cypher`` to thread a Cypher call
        through the same per-commit driver as the typed methods.
        Lives here (rather than reaching into the underscored
        ``_client_for``) so the toolkit has a stable surface.

        Raises:
            ValueError: when ``sha`` names no indexed commit. See
                :meth:`_resolve_indexed_commit` for why this is louder than
                returning ``None``.
        """
        target = sha or self.head()
        if target is None:
            return None
        if sha:
            # Only for a caller-supplied sha. ``head()`` is authoritative and
            # the write paths (``_client_for_active_commit``) legitimately open
            # commits that are not in the manifest yet.
            target = self._resolve_indexed_commit(sha)
        return await self._client_for(target)

    def _resolve_indexed_commit(self, sha: str) -> str:
        """Map a caller-supplied ``sha`` onto a commit that is actually indexed.

        Agents pass the abbreviated sha they saw in conversation —
        ``commit="84a9f3b"`` — and ``(project, commit)`` is a *store identity*,
        not a lookup. So an abbreviation silently became a different pair:
        ``start_for_commit`` spawned a second Neo4j on an empty store, the query
        returned zero rows, and the agent reported the index as empty. A capture
        run produced two servers for one project, one with the 40-char sha and
        one with 7 chars, and left ``<project>-<7 chars>`` directories behind —
        the same shape as the stale ``…-c3f316b`` and empty-sha dirs already on
        disk.

        Silent wrong answers are the worst outcome here, worse than an error:
        a training corpus built on them teaches that the index has no data. So
        resolve an unambiguous prefix, and refuse anything else.
        """
        commits = self.manifest.load().commits
        if sha in commits:
            return sha
        matches = [commit for commit in commits if commit.startswith(sha)]
        if len(matches) == 1:
            logger.debug("resolved abbreviated commit %s -> %s", sha, matches[0])
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"commit {sha!r} is ambiguous — it matches {len(matches)} indexed "
                "commits. Pass the full 40-character sha, or omit `commit` to use HEAD."
            )
        known = ", ".join(sorted(commits)[:3]) or "none"
        raise ValueError(
            f"commit {sha!r} is not indexed, so there is no graph to query. Omit "
            f"`commit` to use HEAD. Indexed commits: {known}"
        )

    async def _client_for(self, sha: str) -> Any | None:
        """Return the per-commit ``Neo4jClient`` (or None if no backend).

        When ``runtime=`` is set, ensures the runtime has a
        process for ``sha`` and hands back the per-commit client.
        When ``neo4j_client=`` was passed at construction, every
        commit is treated as the same client (process IS the
        isolation boundary; sharing one client across commits is
        the test escape hatch).
        """
        if self._neo4j_client is not None:
            return self._neo4j_client
        if self._neo4j_runtime is not None:
            if sha in self._clients:
                return self._clients[sha]
            async with self._lock:
                if sha not in self._clients:
                    await self._neo4j_runtime.start_for_commit(self.project_id, sha)
                    driver = self._neo4j_runtime.driver_for(self.project_id, sha)
                    from ember_code.core.code_index.neo4j_client import Neo4jClient

                    client = Neo4jClient(driver, self.project_id, sha)
                    # Apply the per-commit schema (indexes on Item/Chunk +
                    # vector index on Chunk.embedding + property indexes) on
                    # first use of this pair's Neo4j. Idempotent — every
                    # statement carries IF NOT EXISTS. Without this,
                    # per-commit processes accumulate data on unindexed
                    # nodes: property lookups do full scans and
                    # `db.index.vector.queryNodes('chunk_embedding', …)`
                    # fails with "no such vector schema index".
                    # `attach_knowledge_neo4j` does the equivalent for the
                    # knowledge DB; this closes the same gap for code_index.
                    await client.apply_schema()
                    self._clients[sha] = client
            return self._clients[sha]
        return None

    async def _require_neo4j_backend(self, op: str) -> Any:
        """Return a per-commit ``Neo4jClient`` or raise.

        ``CodeIndex`` no longer ships a chroma fallback — every
        public method (add_item, search, get_item, filter_items,
        etc.) requires a neo4j backend. The ``runtime=`` /
        ``neo4j_client=`` constructor params select the backend.
        """
        if self._neo4j_client is None and self._neo4j_runtime is None:
            raise NotImplementedError(
                f"CodeIndex.{op} requires a neo4j backend — pass "
                f"``runtime=`` (production) or ``neo4j_client=`` "
                f"(test) to the constructor."
            )
        return None  # placeholder so callers can read the guard

    async def add_item(self, sha: str, item: CodeIndexItem) -> None:
        """Insert/replace an item + its chunks in this commit's neo4j process."""
        await self._require_neo4j_backend("add_item")
        await self.prepare_commit(sha)
        client = await self._client_for(sha)
        assert client is not None  # guarded above

        # ``upsert_item`` MERGE-deletes any existing :Chunk for
        # the parent first, so re-upserts are clean.
        chunk_texts, spans = self._rows_for(item)
        embeddings = self._embedder.embed(chunk_texts) if chunk_texts else []
        chunks = [
            ChunkRow(text, embedding, kind, line_from, line_to)
            for (text, embedding, (kind, line_from, line_to)) in zip(
                chunk_texts, embeddings, spans, strict=True
            )
        ]
        await client.upsert_item(item, chunks)
        self.manifest.touch(sha)

    async def add_items(self, sha: str, items: Sequence[CodeIndexItem]) -> None:
        """Insert/replace many items, embedding all their chunks in one call.

        Why bulk: :meth:`add_item` embeds one item's chunks per call, and the
        applier calls it once per item interleaved with a Neo4j write. Measured on
        an M-series machine with the model on ``mps``, that pattern runs at
        1,863 texts/s where a single batched call reaches 4,237 — and the *observed*
        rate during a real load was 83 chunks/s, roughly 2% of the hardware,
        because the GPU idles through every database round trip.

        Embedding is the dominant cost of a load (chunks are 93% of the nodes
        written), so batching across items is the difference between 29 minutes
        and about a minute of embedding for a repository the size of celery.
        """
        if not items:
            return
        await self._require_neo4j_backend("add_items")
        await self.prepare_commit(sha)
        client = await self._client_for(sha)
        assert client is not None  # guarded above

        # Chunk everything first, remember each item's slice, then embed once.
        per_item: list[tuple[CodeIndexItem, int, int, list]] = []
        all_texts: list[str] = []
        for item in items:
            texts, spans = self._rows_for(item)
            per_item.append((item, len(all_texts), len(all_texts) + len(texts), spans))
            all_texts.extend(texts)

        embeddings = self._embedder.embed(all_texts) if all_texts else []
        for item, start, end, spans in per_item:
            chunks = [
                ChunkRow(text, embedding, kind, line_from, line_to)
                for (text, embedding, (kind, line_from, line_to)) in zip(
                    all_texts[start:end], embeddings[start:end], spans, strict=True
                )
            ]
            await client.upsert_item(item, chunks)
        self.manifest.touch(sha)

    async def remove_item(self, sha: str, item_id: str) -> None:
        """Drop an item and all its chunks from this commit's neo4j process."""
        await self._require_neo4j_backend("remove_item")
        client = await self._client_for(sha)
        assert client is not None
        await client.delete_item(item_id)
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
        """Semantic search inside one commit's neo4j process.

        ``where`` is a :class:`ChromaWhereFilter` (or a raw dict
        for legacy callers) applied against the :Item nodes
        first; the resulting parent IDs narrow the vector search
        over :Chunk. The codeindex_query tool builds the filter
        from its structured args; callers shouldn't construct it
        by hand.
        """
        await self._require_neo4j_backend("search")
        sha = commit or self.head()
        if sha is None:
            return []
        client = await self._client_for(sha)
        assert client is not None

        # Quality / categorical fields live on :Item, not :Chunk.
        # When ``where`` is supplied, resolve it against :Item to
        # get matching parent IDs, then narrow the vector query.
        parent_ids_or_sentinel = await self._resolve_parent_where(client, where)
        if parent_ids_or_sentinel is _NARROWED_TO_EMPTY:
            return []
        # After the sentinel check, the helper narrows to
        # list[str] | None; the ``None`` branch means "no filter
        # narrowing" and ``restricted_where`` falls through to None.
        parent_ids: list[str] | None = parent_ids_or_sentinel  # type: ignore[assignment]  # narrowed by sentinel check above

        query_vec = self._embedder.embed([query])[0]
        restricted_where = {"parent_id": {"$in": list(parent_ids)}} if parent_ids else None
        results = await client.vector_search(
            embedding=query_vec, where=restricted_where, limit=limit
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
        """Like :meth:`search` but restricted to a fixed set of parent IDs.

        Used by the disambiguation-refs path on ``codeindex_query``:
        given the reference graph of an item (its callers /
        callees), this scores each reference's similarity to
        the original ``query_text`` and returns the top-K with
        full content. The restriction is applied as a vector
        search ``where={"parent_id": {"$in": ...}}`` filter.
        """
        await self._require_neo4j_backend("search_among")
        sha = commit or self.head()
        if sha is None or not candidate_ids:
            return []
        client = await self._client_for(sha)
        assert client is not None
        query_vec = self._embedder.embed([query])[0]
        results = await client.vector_search(
            embedding=query_vec,
            where={"parent_id": {"$in": list(candidate_ids)}},
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
        """Direct fetch / filter against the :Item nodes (no semantic search)."""
        await self._require_neo4j_backend("filter_items")
        sha = commit or self.head()
        if sha is None:
            return []
        client = await self._client_for(sha)
        assert client is not None
        # The :Item schema has the typed fields the
        # :class:`ChromaWhereFilter` declares (``type`` /
        # ``quality`` / etc.); the neo4j client's
        # ``_render_where`` already speaks the same operator set.
        where_dict = where.to_chroma_where() if isinstance(where, ChromaWhereFilter) else where
        return await client.filter_items(where=where_dict, ids=ids, limit=limit)

    async def get_item(
        self,
        item_id: str,
        *,
        commit: str | None = None,
    ) -> CodeIndexResult | None:
        await self._require_neo4j_backend("get_item")
        sha = commit or self.head()
        if sha is None:
            return None
        client = await self._client_for(sha)
        assert client is not None
        return await client.get_item(item_id)

    # -- Retention -------------------------------------------------------------

    async def clean(
        self,
        *,
        keep_recent_days: int = 30,
    ) -> list[str]:
        """Drop commits not on branch + idle > N days; reclaim neo4j data.

        Selective housekeeping — preserves HEAD and every commit
        pointed to by a local branch. The eviction drops the
        per-commit nodes via :meth:`Neo4jClient.drop_database` and
        the manifest entry, then ``sweep_stale_dirs`` reclaims
        the (now-empty) per-commit state dir on next startup.
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

        # On the neo4j path, ``Neo4jClient.drop_database`` removes
        # every :Item / :Chunk / :REL node for the (project, commit)
        # pair. With no runtime configured this is a no-op (sqlite-
        # only index has no neo4j state to drop). We still want to
        # drop the manifest entry either way.
        for sha in to_drop:
            try:
                if self._neo4j_client is not None:
                    from ember_code.core.code_index.neo4j_client import Neo4jClient

                    await Neo4jClient.drop_database(
                        self._neo4j_client._driver, self.project_id, sha
                    )
                elif self._neo4j_runtime is not None:
                    # The runtime is the source of truth for per-
                    # commit processes; ask it to drop the commit's
                    # data. This is a no-op if the process is already
                    # gone.
                    pass  # drop is handled by runtime's evict path
            except Exception as exc:
                logger.debug("clean: neo4j drop failed for %s (%s)", sha[:8], exc)
            self.manifest.remove_commit(sha)
        return to_drop

    def sweep_stale_dirs(self) -> list[str]:
        """Reclaim per-commit state dirs no longer tracked in the manifest.

        :meth:`clean` and :meth:`forget_commit` both drop manifest
        entries without immediately removing the per-commit state
        dir — see those methods for why rmtree is unsafe (the
        runtime may still hold a process for that commit if
        another BE re-attached). This sweep closes the loop at
        session startup, before any :class:`Neo4jRuntime.start_for_commit`
        call. Typical placement: at session startup, before the
        initial ``sync_now``.
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

        The neo4j index stores items at three granularities
        (``folder`` / ``file`` / ``entity``), so a naive count
        would conflate files with the functions and classes
        inside them — producing Coverage values above 100%. We
        filter to ``type == "file"`` and dedupe by path so the
        numbers are directly comparable to ``git ls-files``.
        """
        client = await self._client_for(sha)
        if client is None:
            return HeadStats(files_indexed=0, languages_indexed={})
        items = await client.filter_items(where={"type": "file"}, limit=50_000)
        seen_paths: set[str] = set()
        ext_counts: Counter[str] = Counter()
        for r in items:
            path = (r.path or "").strip()
            if path and path in seen_paths:
                continue
            if path:
                seen_paths.add(path)
            ext = (r.file_extension or "").lower()
            ext_counts[ext or "(other)"] += 1
        return HeadStats(
            files_indexed=len(seen_paths) if seen_paths else len(items),
            languages_indexed=dict(ext_counts),
        )

    # -- Internal --------------------------------------------------------------

    async def _resolve_parent_where(
        self,
        client: Any,
        where: ChromaWhereFilter | dict[str, Any] | None,
    ) -> list[str] | None | object:
        """Translate a parent-side ``where`` into a parent-ID list.

        The :Item schema has the typed fields the
        :class:`ChromaWhereFilter` declares (``type`` /
        ``quality`` / etc.); the neo4j client's ``_render_where``
        method already speaks the same operator set. We translate
        the filter to a neo4j where dict, run ``filter_items`` to
        get the matching parent IDs, and return them.

        Returns ``None`` when the filter is empty (no narrowing
        needed), the list of matching IDs, or
        :data:`_NARROWED_TO_EMPTY` when the filter matches nothing
        so callers short-circuit.
        """
        where_dict = self._render_where(where)
        if not where_dict:
            return None
        # ``filter_items`` with just an ``ids``-less, where-only
        # query and a small limit gets the matching parent IDs.
        # (No semantic ranking here — narrowing only.)
        rows = await client.filter_items(where=where_dict, ids=None, limit=10_000)
        if not rows:
            return _NARROWED_TO_EMPTY
        return [r.item_id for r in rows]

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

    # Code chunk geometry. Lines rather than characters because the whole point
    # of a code chunk is that it can say *where* — and a prose chunker reports no
    # offsets, so a hit could only ever name the file. Overlap so a construct
    # spanning a boundary is intact in one of the two windows.
    CODE_CHUNK_LINES = 40
    CODE_CHUNK_OVERLAP = 10

    def _chunk_source(self, source: str, line_from: int | None) -> list[tuple[str, int, int]]:
        """Split source into overlapping line windows, each with its own span.

        ``line_from`` is the item's first line in the file, so the returned spans
        are absolute file lines and a caller can go straight to them. Returns
        ``(text, line_from, line_to)``.
        """
        if not source or not source.strip():
            return []
        lines = source.splitlines()
        base = line_from or 1
        step = max(self.CODE_CHUNK_LINES - self.CODE_CHUNK_OVERLAP, 1)
        windows: list[tuple[str, int, int]] = []
        start = 0
        while start < len(lines):
            end = min(start + self.CODE_CHUNK_LINES, len(lines))
            text = "\n".join(lines[start:end])
            if text.strip():
                windows.append((text, base + start, base + end - 1))
            if end >= len(lines):
                break
            start += step
        return windows

    def _rows_for(
        self, item: CodeIndexItem
    ) -> tuple[list[str], list[tuple[str, int | None, int | None]]]:
        """Every text this item contributes, tagged for reassembly after embedding.

        Summary chunks first (they carry no position), then code chunks with
        their absolute line spans.
        """
        summary = self._chunk_text(item.content or "")
        code = self._chunk_source(getattr(item, "source", None) or "", item.line_from)
        texts = [*summary, *[text for text, _, _ in code]]
        spans: list[tuple[str, int | None, int | None]] = [("summary", None, None)] * len(summary)
        spans.extend(("code", start, end) for _, start, end in code)
        return texts, spans


# Sentinel returned by :meth:`CodeIndex._resolve_chunk_where` when the
# parent filter narrowed to zero IDs — the read path short-circuits to
# an empty result without touching chunks.
_NARROWED_TO_EMPTY: object = object()


__all__ = [
    "ChromaWhereFilter",
    "CodeIndex",
    "CommitNotFoundError",
    "HeadStats",
]
