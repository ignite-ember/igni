"""Knowledge-base RPCs — :class:`KnowledgeController` owns status /
search / add / list / get / remove / auto-sync for one session.

Wire types (``KnowledgeHit``, ``KnowledgeListEntry``,
``KnowledgeGetResult``, ``KnowledgeRemoveResult``,
``KnowledgeStatus``) are declared in :mod:`schemas_knowledge` and
re-exported here so external callers keep the previous import
surface.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ember_code.backend.schemas_knowledge import (
    KnowledgeGetResult,
    KnowledgeHit,
    KnowledgeListEntry,
    KnowledgeRemoveResult,
    KnowledgeStatus,
)
from ember_code.core.knowledge.models import KnowledgeIndexEntry
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)

# Re-export the schemas at this module's top-level so callers that
# used to do ``from server_knowledge import KnowledgeHit`` still
# resolve.
__all__ = [
    "KnowledgeController",
    "KnowledgeHit",
    "KnowledgeListEntry",
    "KnowledgeGetResult",
    "KnowledgeRemoveResult",
    "KnowledgeStatus",
]


class KnowledgeController:
    """Knowledge-base RPCs for one :class:`Session`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def status(self) -> KnowledgeStatus:
        """Status snapshot for the knowledge panel header."""
        status = await self._session.knowledge_mgr.status()
        return KnowledgeStatus(
            enabled=status.enabled,
            collection_name=status.collection_name,
            document_count=status.document_count,
            embedder=status.embedder,
        )

    async def search(self, query: str) -> list[KnowledgeHit]:
        """Search the knowledge base — one :class:`KnowledgeHit` per
        result."""
        response = await self._session.knowledge_mgr.search(query)
        return [
            KnowledgeHit(
                name=r.name,
                content=r.content,
                score=r.score,
                metadata={k: str(v) for k, v in r.metadata.items()},
            )
            for r in response.results
        ]

    async def add(self, source: str) -> msg.Info:
        """Add content to the knowledge base from the panel."""
        mgr = self._session.knowledge_mgr
        if source.startswith(("http://", "https://")):
            result = await mgr.add_url(source)
        elif "/" in source or source.startswith("."):
            result = await mgr.add_path(source)
        else:
            result = await mgr.add(text=source)
        if not result.success:
            return msg.Info(text=result.error or "Add failed.")
        return msg.Info(text=result.message)

    async def list(self) -> list[KnowledgeListEntry]:
        """Every document in the KB — used by the panel's Browse tab."""
        knowledge = self._session.knowledge_mgr.knowledge
        if knowledge is None:
            return []
        try:
            entries = await knowledge.list_entries()
        except Exception as exc:
            logger.debug("knowledge_list failed: %s", exc)
            return []

        out: list[KnowledgeListEntry] = []
        for e in entries:
            content = e.content
            meta = e.metadata
            out.append(
                KnowledgeListEntry(
                    id=e.id,
                    name=self._name_for(e),
                    source=e.source,
                    size=len(content),
                    preview=content[:240],
                    added_at=str(meta.get("added_at", "")),
                    kind=str(meta.get("kind", "")),
                    metadata={k: str(v) for k, v in meta.items() if v is not None},
                )
            )
        out.sort(key=lambda d: d.added_at, reverse=True)
        return out

    async def get(self, entry_id: str) -> KnowledgeGetResult:
        """Full content for one document — used by the detail page."""
        knowledge = self._session.knowledge_mgr.knowledge
        if knowledge is None:
            return KnowledgeGetResult(error="Knowledge base is disabled.")
        try:
            entries = await knowledge.list_entries()
        except Exception as exc:
            return KnowledgeGetResult(error=f"knowledge_get failed: {exc}")
        match = next((e for e in entries if e.id == entry_id), None)
        if not match:
            return KnowledgeGetResult(error=f"Document {entry_id} not found.")
        meta = match.metadata
        return KnowledgeGetResult(
            id=entry_id,
            name=(match.source or "").strip() or entry_id,
            source=match.source,
            content=match.content,
            metadata={k: str(v) for k, v in meta.items() if v is not None},
        )

    async def remove(self, entry_id: str) -> KnowledgeRemoveResult:
        """Delete one document by id."""
        knowledge = self._session.knowledge_mgr.knowledge
        if knowledge is None:
            return KnowledgeRemoveResult(removed=False, error="Knowledge base is disabled.")
        try:
            removed = await knowledge.delete_entry(entry_id)
            return KnowledgeRemoveResult(removed=removed)
        except Exception as exc:
            return KnowledgeRemoveResult(removed=False, error=str(exc))

    async def auto_sync(self) -> str | None:
        """Auto-sync knowledge file on startup. Returns status
        message or None."""
        if self._session.knowledge is None:
            return None
        try:
            result = await self._session.knowledge_mgr.sync_from_file()
            if result:
                return f"Knowledge synced: {result}"
        except Exception as exc:
            logger.debug("knowledge sync_from_file failed (%s)", exc)
        return None

    @staticmethod
    def _name_for(entry: KnowledgeIndexEntry) -> str:
        """Best display label for a KB entry — source basename, or
        first non-empty line of content for inline text."""
        source = (entry.source or "").strip()
        if source:
            if source.startswith(("http://", "https://")):
                return source
            return PurePosixPath(source).name or source
        content = (entry.content or "").strip()
        for line in content.splitlines():
            line = line.strip().lstrip("# ").strip()
            if line:
                return line[:80]
        return "(untitled)"
