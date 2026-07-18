"""Chroma client + collection lifecycle policy for CodeIndex.

Owns:
  - Client construction (:meth:`open`) — one place the ``import chromadb``
    lives, no more inline imports scattered through ``index.py``.
  - HNSW recall/latency contract (:attr:`HNSW_METADATA`) — one place to
    bump the ``hnsw:*`` knobs. Kept in lockstep with
    ``core.knowledge.index``'s parallel factory; bump both together
    or one index's recall regresses.
  - The two-phase teardown workaround for
    ``SQLITE_READONLY_DBMOVED`` (see :meth:`drop_commit_collections`).

Deliberately does **not** cache clients — :class:`CodeIndex` keeps its
own per-sha cache under ``self._clients`` and its own asyncio lock. The
factory only constructs and hands back. Introducing a factory-level
cache would double-cache under chromadb's own process-level cache and
break the two-phase teardown semantics that ``forget_commit`` and
``clean`` depend on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import chromadb

from ember_code.core.embeddings import EmbeddingFunction

logger = logging.getLogger(__name__)

DOCUMENTS_COLLECTION = "code_index_documents"
CHUNKS_COLLECTION = "code_index_chunks"


class ChromaClientFactory:
    """Constructs Chroma clients + collections with the CodeIndex HNSW config.

    HNSW knobs (chroma supports the ``hnsw:*`` metadata keys):
      - ``hnsw:space=cosine`` — explicit; the score-as-1-minus-distance
        formula assumes cosine.
      - ``hnsw:M=32`` — graph connectivity. Higher M = better graph
        topology = better recall at any search_ef. 32 is the sweet spot
        for most embedding sizes; doubles memory but our scale is tiny.
      - ``hnsw:construction_ef=400`` — graph build quality. Higher =
        better graph at index time, paid once.
      - ``hnsw:search_ef=10000`` — per-query candidate pool size. Higher
        = better recall at query time, paid every query. At 10000 with
        ~42k chunks, recall on top-K is ~100% for K up to ~100.

    NOTE — this factory is the single source of truth for the HNSW
    config across BOTH indexes. :class:`ember_code.core.knowledge.index.KnowledgeIndex`
    composes an instance of this class to open its knowledge chroma
    dir; ``scripts/reindex_hnsw.py`` imports :attr:`HNSW_METADATA`
    directly. Bump the values here and both call sites pick it up on
    the next open.
    """

    HNSW_METADATA: dict[str, Any] = {
        "hnsw:space": "cosine",
        "hnsw:M": 32,
        "hnsw:construction_ef": 400,
        "hnsw:search_ef": 10000,
    }

    DOCUMENTS_NAME: str = DOCUMENTS_COLLECTION
    CHUNKS_NAME: str = CHUNKS_COLLECTION

    def open(self, path: Path) -> Any:
        """Open a chromadb persistent client rooted at ``path``.

        chromadb keeps a process-level client cache keyed on path — the
        second call with the same path returns the previously-cached
        client. That behavior is what makes the two-phase teardown in
        :meth:`drop_commit_collections` safe.
        """
        return chromadb.PersistentClient(path=str(path))

    def get_or_create(self, client: Any, name: str) -> Any:
        """Get or create a chroma collection with the recall-tuned HNSW config.

        Chroma defaults to ``hnsw:search_ef=10`` which gives broken recall
        on top-K queries with K > ~10 (the index returns near-floor matches
        instead of the truly closest neighbors). At our scale (a few tens
        of thousands of chunks) HNSW with high search_ef is effectively
        exact, latency-cheap, and matches the precision the agent expects.

        These parameters are baked into the collection's metadata at
        creation time. Existing collections created without them keep
        chroma's defaults until rebuilt — see ``scripts/reindex_hnsw.py``.
        """
        return client.get_or_create_collection(
            name=name,
            embedding_function=EmbeddingFunction(),
            metadata=dict(self.HNSW_METADATA),
        )

    def docs_and_chunks(self, client: Any) -> tuple[Any, Any]:
        """Return ``(documents, chunks)`` collections for one client.

        Convenience — used by every read path on :class:`CodeIndex`.
        """
        docs = self.get_or_create(client, self.DOCUMENTS_NAME)
        chunks = self.get_or_create(client, self.CHUNKS_NAME)
        return docs, chunks

    def drop_commit_collections(self, client: Any) -> None:
        """Empty a commit's collections via chromadb's own API.

        The two-phase teardown workaround for ``SQLITE_READONLY_DBMOVED``
        (1032 — "database file was moved out from under it"): chromadb
        keeps a process-level client cache, so a directory ``rmtree``-d
        underneath a live client leaves a stale SQLite handle and the
        next write fails. Going through
        ``client.delete_collection`` keeps chromadb's connection state
        consistent; the snapshot apply then re-creates the collections
        via ``get_or_create_collection`` and re-fills them.

        Best-effort — chroma raises on missing collections, and callers
        (both :meth:`forget_commit` and :meth:`clean`) want the teardown
        to keep going. Exceptions are logged at debug level; the shape
        of the log line is uniform for both callers.
        """
        for name in (self.DOCUMENTS_NAME, self.CHUNKS_NAME):
            try:
                client.delete_collection(name)
            except Exception as exc:
                # Missing collection / chroma rejection is fine — we
                # only care that nothing's left to write into.
                logger.debug(
                    "drop_commit_collections: delete_collection(%s) skipped (%s)", name, exc
                )
