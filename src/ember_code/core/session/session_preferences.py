"""Per-session preferences — durable across restarts.

Holds preferences that should be remembered when a session is
resumed via ``--continue``. Today only the active model name; the
table is keyed by ``session_id`` so other per-session knobs can be
added as additional columns without a migration story.

User-level defaults still live in ``~/.ember/config.yaml`` (written
by ``UserConfigStore.set_default_model``); this store layers on top
so a resumed session keeps the model it was last using even if the
user has since picked a different default for new sessions.

Lives in the same project-local ``state.db`` Agno uses, so no new
file is needed. ``CREATE TABLE IF NOT EXISTS`` runs on first use;
existing databases pick up the table on next launch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from pathlib import Path

from ember_code.core.session._sqlite_utils import connect_kv

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ember_session_preferences (
    session_id TEXT PRIMARY KEY,
    model_name TEXT,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
"""


class SessionPreferencesStore:
    """SQLite-backed key/value store for per-session preferences.

    Writes are tiny (one row, single column update) so the sync API
    is exposed directly. Async wrappers exist for hot paths invoked
    from the event loop.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return connect_kv(self._db_path)

    def set_model(self, session_id: str, model_name: str) -> None:
        """Upsert the model preference for ``session_id``.

        Best-effort: persistence failures are logged but do not
        propagate. The in-memory switch has already taken effect by
        the time we get here — the only consequence of a failed save
        is that ``--continue`` won't restore this specific choice.
        """
        try:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO ember_session_preferences (session_id, model_name) "
                    "VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "model_name=excluded.model_name, "
                    "updated_at=strftime('%s', 'now')",
                    (session_id, model_name),
                )
        except Exception as exc:
            logger.debug("set_model failed for %s: %s", session_id, exc)

    def get_model(self, session_id: str) -> str | None:
        """Return the persisted model for ``session_id`` or None."""
        try:
            with contextlib.closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT model_name FROM ember_session_preferences WHERE session_id=?",
                    (session_id,),
                ).fetchone()
        except Exception as exc:
            logger.debug("get_model failed for %s: %s", session_id, exc)
            return None
        if row is None:
            return None
        return row["model_name"]

    # ── Async wrappers ────────────────────────────────────────────

    async def aset_model(self, session_id: str, model_name: str) -> None:
        await asyncio.to_thread(self.set_model, session_id, model_name)

    async def aget_model(self, session_id: str) -> str | None:
        return await asyncio.to_thread(self.get_model, session_id)
