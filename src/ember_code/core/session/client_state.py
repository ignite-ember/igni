"""Per-client UI state.

The web UI, the JetBrains plugin, and the VSCode extension all need a
small amount of state that survives across reloads: which session a
window is bound to, sidebar collapsed/open, composer drafts, future
toggles. Each client stores nothing locally except a stable
``client_id``; everything else lives in this SQLite table so the
behaviour is identical regardless of the host.

The store is keyed by ``(client_id, key)`` — clients enumerate their
own state with a single ``get_for_client(client_id)`` call and write
individual entries with ``set_value``.
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
CREATE TABLE IF NOT EXISTS ember_client_state (
    client_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (client_id, key)
);
"""


class ClientStateStore:
    """SQLite-backed (client_id, key) → value KV store (global)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    @classmethod
    def from_data_dir(cls, data_dir: str | Path) -> ClientStateStore:

        return cls(data_root(data_dir) / "client_state.db")

    def _connect(self) -> sqlite3.Connection:
        return connect_kv(self._db_path)

    def get_for_client(self, client_id: str) -> dict[str, str]:
        if not client_id:
            return {}
        try:
            with contextlib.closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT key, value FROM ember_client_state WHERE client_id=?",
                    (client_id,),
                ).fetchall()
        except Exception as exc:
            logger.debug("client_state get_for_client failed: %s", exc)
            return {}
        return {r["key"]: r["value"] for r in rows}

    def set_value(self, client_id: str, key: str, value: str) -> None:
        if not client_id or not key:
            return
        try:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO ember_client_state (client_id, key, value) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(client_id, key) DO UPDATE SET "
                    "value=excluded.value, "
                    "updated_at=strftime('%s', 'now')",
                    (client_id, key, value),
                )
        except Exception as exc:
            logger.debug("client_state set_value failed: %s", exc)

    def delete_value(self, client_id: str, key: str) -> None:
        if not client_id or not key:
            return
        try:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "DELETE FROM ember_client_state WHERE client_id=? AND key=?",
                    (client_id, key),
                )
        except Exception as exc:
            logger.debug("client_state delete_value failed: %s", exc)
