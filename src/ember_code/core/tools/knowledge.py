"""KnowledgeTools — agent-facing operations on the project knowledge base."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.tools import Toolkit

if TYPE_CHECKING:
    from ember_code.core.session.knowledge_ops import SessionKnowledgeManager

logger = logging.getLogger(__name__)

#: How many entries one ``knowledge_delete`` call can remove. Mirrors
#: :meth:`KnowledgeIndex.delete_by_query`'s own default so the preview
#: and the delete see the same rows; passed explicitly rather than left
#: to the default so the two cannot drift apart silently.
_DELETE_LIMIT = 10


class KnowledgeTools(Toolkit):
    """Agent-facing tools for interacting with the knowledge base."""

    def __init__(self, knowledge_mgr: SessionKnowledgeManager):
        super().__init__(name="ember_knowledge")
        self._mgr = knowledge_mgr
        self.register(self.knowledge_search)
        self.register(self.knowledge_add)
        self.register(self.knowledge_delete)
        self.register(self.knowledge_status)

    async def knowledge_search(self, query: str, limit: int = 5) -> str:
        """Search the knowledge base for relevant information.

        Args:
            query: Natural-language search query.
            limit: Max results to return (default 5).
        """
        response = await self._mgr.search(query, limit=limit)
        if not response.results:
            return f"No knowledge found for: {query}"
        lines = [f"Found {response.total} result(s):"]
        for i, r in enumerate(response.results, 1):
            name = r.name or "untitled"
            lines.append(f"\n{i}. [{name}]\n{r.content}")
        return "\n".join(lines)

    async def knowledge_add(self, content: str, source: str = "") -> str:
        """Store new knowledge in the knowledge base.

        Use when you discover important information that should be
        remembered for future tasks — patterns, decisions, context, etc.

        Args:
            content: The knowledge content to store.
            source: Optional source description (e.g. file path, URL).
        """
        metadata = {"source": source} if source else None
        result = await self._mgr.add(text=content, metadata=metadata)
        if not result.success:
            return f"Error: {result.error}"
        return result.message

    async def knowledge_delete(self, query: str, confirm: bool = False) -> str:
        """Delete knowledge entries matching a search query.

        First searches for matching entries, then deletes them. Set
        ``confirm=True`` to actually delete; without it, returns a
        preview listing the entries that would go.

        At most ``_DELETE_LIMIT`` entries are deleted per call — the
        preview shows exactly that set, so a query matching more than
        the cap has to be re-run rather than silently half-deleting.

        Args:
            query: Search query to find entries to delete.
            confirm: If True, actually delete. If False, preview only.
        """
        if not confirm:
            return await self._delete_preview(query)
        if self._mgr.knowledge is None:
            return "Error: Knowledge base not available."
        try:
            result = await self._mgr.knowledge.delete_by_query(query, limit=_DELETE_LIMIT)
        except Exception as e:
            return f"Error deleting entries: {e}"
        if result.deleted == 0:
            return "No entries found to delete."
        return f"Deleted {result.deleted} knowledge entry/entries matching '{query}'."

    async def _delete_preview(self, query: str) -> str:
        """Render the rows ``knowledge_delete(confirm=True)`` would remove.

        The preview used to be a canned sentence that never touched the
        index, so "preview" could not tell zero matches from fifty — the
        agent had nothing to decide on and confirmed blind. Searching
        with the same limit ``delete_by_query`` uses means the list shown
        is the list that goes.
        """
        if self._mgr.knowledge is None:
            # Same answer the confirm branch gives. Without this the
            # search below returns an empty response and the preview
            # would report "nothing matches" for a base that is simply
            # not there.
            return "Error: Knowledge base not available."
        response = await self._mgr.search(query, limit=_DELETE_LIMIT)
        if not response.results:
            return f"Preview mode: no knowledge entries match '{query}'. Nothing would be deleted."
        lines = [
            f"Preview mode: {len(response.results)} entry/entries match '{query}' "
            f"and would be deleted (at most {_DELETE_LIMIT} per call):"
        ]
        for i, r in enumerate(response.results, 1):
            name = r.name or "untitled"
            snippet = " ".join(r.content.split())[:200]
            lines.append(f"\n{i}. [{name}]\n{snippet}")
        lines.append("\nCall knowledge_delete again with confirm=True to delete them.")
        return "\n".join(lines)

    async def knowledge_status(self) -> str:
        """Check the current state of the knowledge base."""
        status = await self._mgr.status()
        if not status.enabled:
            return "Knowledge base is disabled."
        return (
            f"Knowledge base: {status.collection_name}\n"
            f"Documents: {status.document_count}\n"
            f"Embedder: {status.embedder}"
        )
