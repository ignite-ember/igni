"""Session knowledge operations — wraps :class:`KnowledgeIndex` for the session.

Folds in YAML mirroring on ``add(...)``, returns the higher-level result
models the tools/CLI consume, and exposes the URL/path ingestion path.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.core.config.settings import Settings
from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.knowledge.ingest import Ingester
from ember_code.core.knowledge.models import (
    KnowledgeAddResult,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    KnowledgeStatus,
    KnowledgeSyncResult,
)
from ember_code.core.knowledge.sync import KnowledgeSyncer

logger = logging.getLogger(__name__)


class SessionKnowledgeManager:
    """Per-session knowledge manager backed by :class:`KnowledgeIndex`."""

    def __init__(
        self,
        knowledge: KnowledgeIndex | None,
        settings: Settings,
        project_dir: Path,
    ):
        self.knowledge = knowledge
        self.settings = settings
        self.project_dir = project_dir

    def share_enabled(self) -> bool:
        return (
            self.settings.knowledge.enabled
            and self.settings.knowledge.share
            and self.knowledge is not None
        )

    def file_path(self) -> Path:
        return self.project_dir / self.settings.knowledge.share_file

    async def add(
        self,
        *,
        text: str,
        source: str = "",
        metadata: dict[str, str] | None = None,
    ) -> KnowledgeAddResult:
        """Add an entry to the knowledge base."""
        if self.knowledge is None:
            return KnowledgeAddResult.fail(
                "Knowledge base is not enabled. Set knowledge.enabled=true in config."
            )

        display_source = source or f"text ({len(text)} chars)"
        try:
            entry_id = await self.knowledge.add(
                content=text,
                source=display_source,
                metadata=metadata or {},
            )
            if self.share_enabled():
                self._mirror_to_yaml(text=text, source=display_source, entry_id=entry_id)
            return KnowledgeAddResult.ok(f"Added to knowledge base: {display_source}")
        except Exception as exc:
            return KnowledgeAddResult.fail(f"Failed to add content: {exc}")

    async def add_url(
        self,
        url: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> KnowledgeAddResult:
        """Fetch a URL, chunk via the right Agno reader, store the document."""
        if self.knowledge is None:
            return KnowledgeAddResult.fail(
                "Knowledge base is not enabled. Set knowledge.enabled=true in config."
            )
        result = await Ingester(self.knowledge).add_url(url, metadata=metadata)
        return KnowledgeAddResult.from_ingest(result, source_label=url)

    async def add_path(
        self,
        path: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> KnowledgeAddResult:
        """Read a file or directory, chunk via the right Agno reader, store each document."""
        if self.knowledge is None:
            return KnowledgeAddResult.fail(
                "Knowledge base is not enabled. Set knowledge.enabled=true in config."
            )
        result = await Ingester(self.knowledge).add_path(path, metadata=metadata)
        return KnowledgeAddResult.from_ingest(result, source_label=path)

    async def search(
        self,
        query: str,
        limit: int = 5,
        cross_project: bool = False,
    ) -> KnowledgeSearchResponse:
        """Semantic search; defaults to current-project scope only."""
        if self.knowledge is None:
            return KnowledgeSearchResponse(query=query)
        try:
            raw = await self.knowledge.search(query=query, limit=limit, cross_project=cross_project)
        except Exception as exc:
            logger.debug("Knowledge search failed: %s", exc)
            return KnowledgeSearchResponse(query=query)

        results = [self._coerce_hit(r) for r in raw]
        return KnowledgeSearchResponse(query=query, results=results, total=len(results))

    @staticmethod
    def _coerce_hit(r: object) -> KnowledgeSearchResult:
        """Normalize search hits into :class:`KnowledgeSearchResult`.

        The index returns typed hits already, but tests stub the facade
        with dict payloads — accept both. Merges the hit's ``project``
        label into ``metadata`` so downstream consumers (the search
        panel, the agent toolkit) can render a per-project ribbon
        without a separate lookup.
        """
        if isinstance(r, KnowledgeSearchResult):
            merged_meta = {
                **{k: str(v) for k, v in r.metadata.items()},
                **({"project": r.project} if r.project else {}),
            }
            return r.model_copy(update={"metadata": merged_meta})
        # Legacy dict shape (test fixtures, external stubs).
        if isinstance(r, dict):
            project = r.get("project") or ""
            merged_meta = {
                **{k: str(v) for k, v in (r.get("metadata") or {}).items()},
                **({"project": project} if project else {}),
            }
            return KnowledgeSearchResult(
                entry_id=r.get("entry_id", ""),
                content=r.get("content", ""),
                name=r.get("name", "") or r.get("source", ""),
                source=r.get("source", ""),
                project=project,
                parent_content=r.get("parent_content", ""),
                score=r.get("score"),
                metadata=merged_meta,
            )
        return KnowledgeSearchResult()

    async def sync_from_file(self) -> KnowledgeSyncResult:
        if not self.share_enabled():
            return KnowledgeSyncResult(
                direction="file_to_db", message="Knowledge sharing is not enabled."
            )
        try:
            return await KnowledgeSyncer(
                file_path=self.file_path(), knowledge=self.knowledge
            ).sync_file_to_db()
        except Exception as exc:
            return KnowledgeSyncResult(direction="file_to_db", error=str(exc))

    async def sync_to_file(self) -> KnowledgeSyncResult:
        if not self.share_enabled():
            return KnowledgeSyncResult(
                direction="db_to_file", message="Knowledge sharing is not enabled."
            )
        try:
            return await KnowledgeSyncer(
                file_path=self.file_path(), knowledge=self.knowledge
            ).sync_db_to_file()
        except Exception as exc:
            return KnowledgeSyncResult(direction="db_to_file", error=str(exc))

    async def sync_bidirectional(self) -> list[KnowledgeSyncResult]:
        return [await self.sync_from_file(), await self.sync_to_file()]

    async def status(self) -> KnowledgeStatus:
        cfg = self.settings.knowledge
        if self.knowledge is None:
            return KnowledgeStatus(enabled=False)
        try:
            count = await self.knowledge.count()
        except Exception as exc:
            logger.warning("Knowledge status count failed: %s", exc)
            count = 0
        return KnowledgeStatus(
            enabled=True,
            collection_name=cfg.collection_name,
            document_count=count,
            embedder="sentence-transformers:all-MiniLM-L6-v2",
        )

    # -- Internal --------------------------------------------------------------

    def _mirror_to_yaml(self, *, text: str, source: str, entry_id: str) -> None:
        syncer = KnowledgeSyncer(file_path=self.file_path())
        entries = syncer.load_file()
        if any(e.id == entry_id for e in entries):
            return
        entry = syncer.make_entry(content=text, source=source)
        # Caller supplies the canonical entry_id so the mirror row
        # matches the vector-store row exactly — override the
        # content-hash id ``make_entry`` produced by default.
        entry = entry.model_copy(update={"id": entry_id})
        entries.append(entry)
        syncer.save_file(entries)
