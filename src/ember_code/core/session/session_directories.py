"""Global session → project-directory registry.

Sessions can live in different project directories (one BE, N
sessions, each potentially a different repo). Per-project state
(``state.db``) can't answer "which directory does session X belong
to?" — that's the question you ask BEFORE you know the directory —
so this registry is GLOBAL: ``<data_dir>/sessions.db``.

Written whenever a runtime starts or renews its session id; read by
the session pool when lazily resuming a session a view asked for.
Unknown sessions fall back to the BE's boot project dir, which is
exactly the pre-pool behaviour.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path

from ember_code.core.code_index.paths import data_root
from ember_code.core.session._sqlite_utils import connect_kv

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ember_session_directories (
    session_id TEXT PRIMARY KEY,
    project_dir TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
"""


class SessionDirectoryStore:
    """SQLite-backed session_id → project_dir mapping (global)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @classmethod
    def from_data_dir(cls, data_dir: str | Path) -> SessionDirectoryStore:

        return cls(data_root(data_dir) / "sessions.db")

    def _connect(self) -> sqlite3.Connection:

        return connect_kv(self._db_path)

    def set_dir(self, session_id: str, project_dir: str | Path) -> None:
        """Upsert. Best-effort — a failed write only means a later
        resume falls back to the BE's boot directory.

        Implementation note: ``sqlite3.Connection.__exit__`` (the
        context-manager protocol) only commits/rollbacks — it does
        NOT close the connection. Wrapping with
        ``contextlib.closing`` guarantees the underlying handle is
        released immediately, so a hot caller doesn't pile up live
        Connection objects waiting for GC.
        """
        if not session_id:
            return
        try:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO ember_session_directories (session_id, project_dir) "
                    "VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "project_dir=excluded.project_dir, "
                    "updated_at=strftime('%s', 'now')",
                    (session_id, str(project_dir)),
                )
        except Exception as exc:
            logger.debug("set_dir failed for %s: %s", session_id, exc)

    def get_dir(self, session_id: str) -> str | None:
        try:
            with contextlib.closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT project_dir FROM ember_session_directories WHERE session_id=?",
                    (session_id,),
                ).fetchone()
        except Exception as exc:
            logger.debug("get_dir failed for %s: %s", session_id, exc)
            return None
        return row["project_dir"] if row else None
