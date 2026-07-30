"""Session-forking coordinator.

Clones the current session under a fresh ``session_id`` — reads
the source row, mints a new UUID, copies every field
(``session_data`` / ``team_data`` / ``metadata`` / ``runs`` /
``summary``) under the new id with fresh ``created_at`` /
``updated_at`` stamps, optionally renames it, and upserts it.
Memories aren't copied — they're user-scoped on disk so the new
session inherits them automatically.
"""

from __future__ import annotations

import logging
import time
import uuid

from agno.db.base import SessionType

from ember_code.core.session.persistence.db_protocol import AgnoSessionDb
from ember_code.core.session.schemas import ForkResult

logger = logging.getLogger(__name__)


class SessionForker:
    """Coordinator for cloning a session under a new id."""

    def __init__(self, db: AgnoSessionDb | None, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def rebind(self, new_session_id: str) -> None:
        """Retarget at a new session id after ``rotate_id``."""
        self._session_id = new_session_id

    async def fork(self, name: str | None = None) -> ForkResult:
        """Clone the current session under a fresh ``session_id``.

        Returns a :class:`ForkResult` — ``ok=True`` with
        ``new_session_id`` on success, ``ok=False`` with a
        diagnostic ``error`` when the DB is unavailable or the
        source can't be loaded.
        """
        if self._db is None:
            return ForkResult(ok=False, error="session store unavailable")
        try:
            source = await self._db.get_session(
                session_id=self._session_id,
                session_type=SessionType.AGENT,
                deserialize=True,
            )
        except Exception as exc:
            logger.debug("Failed to load source session for fork: %s", exc)
            return ForkResult(ok=False, error=str(exc))
        if source is None:
            return ForkResult(
                ok=False,
                error=f"source session not found: {self._session_id}",
            )
        # Match the 8-char prefix scheme used elsewhere in the
        # codebase (``core.py``'s fresh-session mint). The full
        # ``uuid.uuid4().hex`` form was correct technically but
        # read as a 32-char wall of hex in the UI.
        new_id = str(uuid.uuid4())[:8]
        now = int(time.time())
        # ``source`` is a freshly-loaded copy from the DB — we own
        # it, so mutating in place is safe. Setting ``session_id``
        # to the new value means ``upsert_session`` writes a NEW
        # row keyed by the new id (the original row is untouched).
        source.session_id = new_id
        source.created_at = now
        source.updated_at = now
        if name:
            sd = dict(source.session_data or {})
            sd["session_name"] = name
            source.session_data = sd
        try:
            await self._db.upsert_session(source, deserialize=True)
        except Exception as exc:
            logger.debug("Failed to upsert forked session: %s", exc)
            return ForkResult(ok=False, error=str(exc))
        return ForkResult(ok=True, new_session_id=new_id)
