"""Knowledge-index warmup phase.

Opens the ChromaDB client + collections eagerly on a background
task so the first ``/knowledge`` press doesn't block the user
while ``KnowledgeIndex.start()`` imports chromadb and opens the
on-disk persistent client.

``KnowledgeIndex.start()`` is idempotent and re-entry-safe via
its internal lock, so :meth:`ensure_started` can be called by
on-demand knowledge ops without worrying about double-open.
"""

from __future__ import annotations

import logging

from ember_code.core.session.startup.base import SessionStartupPhase

logger = logging.getLogger(__name__)


class KnowledgeWarmupPhase(SessionStartupPhase):
    """Fire-and-forget knowledge-index warmup + on-demand ensure.

    Reads ``session.knowledge`` at call time — the session may
    have ``knowledge=None`` when the feature is disabled in
    settings; both entry points are no-ops in that case.
    """

    def start_background(self) -> None:
        """Open the chroma client + collections on a background task.

        Without this warmup, the first ``/knowledge`` press blocks
        while ``KnowledgeIndex.start()`` imports chromadb and opens
        the on-disk persistent client. Running it eagerly off the
        event loop lets the session finish booting while the
        warmup happens in parallel.
        """
        session = self.session
        if session.knowledge is None:
            return

        async def _warmup() -> None:
            try:
                await session.knowledge.start()
            except Exception as exc:
                self._log_swallowed(exc, "Knowledge warmup")

        self._schedule_on_loop(_warmup)

    async def ensure_started(self) -> None:
        """Guarantee the chroma client is open before knowledge ops run.

        Cheap once :meth:`start_background` has warmed it —
        ``KnowledgeIndex.start()`` is idempotent and re-entry-safe
        via its internal lock.
        """
        session = self.session
        if session.knowledge is None:
            return
        await session.knowledge.start()
