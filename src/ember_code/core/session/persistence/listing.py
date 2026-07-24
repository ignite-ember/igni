"""Session-listing coordinator.

Owns the Agno session enumeration + the ``agent_id == "ember"``
sub-agent filter that used to sit inline in
``SessionPersistence.list_sessions``. Sub-agents (visualizer,
editor, every specialist in the pool) share the top-level DB so
paused runs can be resumed via ``acontinue_run`` — see
``AgentPool.__init__``'s ``_db`` — but they must NEVER appear as
chats in the user-facing session list. The top-level session is
built with ``Agent(name="ember")`` (see
``_build_main_agent``), so ``agent_id == "ember"`` is the
discriminator. Anything else is scratch.
"""

from __future__ import annotations

import logging

from agno.db.base import SessionType

from ember_code.core.session.persistence.db_protocol import AgnoSessionDb
from ember_code.core.session.schemas import LoadResult, SessionListRow

logger = logging.getLogger(__name__)


class SessionListing:
    """Coordinator for the session-list query."""

    def __init__(self, db: AgnoSessionDb | None) -> None:
        self._db = db

    async def list_sessions(self, limit: int | None = None) -> LoadResult[list[SessionListRow]]:
        """Enumerate top-level sessions for this project.

        ``limit=None`` (the default) returns every session — the FE
        virtualises the list itself. Callers can pass an int to
        cap; the CLI's pre-launch session preview does this to
        keep boot time flat.

        Returns a :class:`LoadResult` — ``ok=True`` on success
        (empty list when no DB or no rows), ``ok=False`` when the
        DB layer raised.
        """
        if self._db is None:
            return LoadResult(ok=True, value=[])
        try:
            sessions = await self._db.get_sessions(
                session_type=SessionType.AGENT,
                limit=limit,
                sort_by="updated_at",
                sort_order="desc",
                deserialize=True,
            )
        except Exception as exc:
            logger.debug("Failed to list sessions: %s", exc)
            return LoadResult(ok=False, value=[], error=str(exc))
        if isinstance(sessions, tuple):
            sessions = sessions[0]
        results: list[SessionListRow] = []
        for s in sessions:
            agent_id = getattr(s, "agent_id", "") or ""
            # Defensive: rows written by earlier BE versions might
            # have ``agent_id == ""`` or missing entirely. Those
            # must NOT be filtered out — otherwise a user could
            # lose access to historical chats.
            if agent_id and agent_id != "ember":
                continue
            results.append(SessionListRow.from_agno(s))
        return LoadResult(ok=True, value=results)
