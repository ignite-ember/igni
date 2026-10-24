"""Notifications an extension told this machine, and whether they were read.

igni server's ``docs/service-identity.md`` §10.2. "Your export is ready"
concerns the person at this keyboard and nobody else, so it never
leaves the machine: the notice and its read state are a row in a
SQLite file under igni's data directory, and nothing here talks to
igni server.

**Once per event.** An extension that retries, or restarts and says the
same thing again, names the event with ``event_key``; a second notice
with the same key from the same extension is dropped, read or not.

**Bounded.** The newest :attr:`LocalNotificationStore.KEEP` are kept per
extension. A process that notifies in a loop costs a fixed amount of
disk, not an unbounded one.
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ember_code.core.data_paths import data_root
from ember_code.core.session._sqlite_utils import connect_kv
from ember_code.notifications.schemas import (
    ExtensionNotice,
    FloorNotification,
    NotificationCounts,
    NotificationOrigin,
    NotificationTarget,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_notifications (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    target      TEXT NOT NULL,
    event_key   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    read_at     TEXT,
    UNIQUE (source, event_key)
)
"""


class LocalNotificationStore:
    """The SQLite file local notifications live in."""

    #: Kept per extension; older ones are dropped as new ones arrive.
    KEEP = 200
    #: The prefix every local id carries, so it can never be mistaken
    #: for one igni server issued.
    ID_PREFIX = "local-"

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(_SCHEMA)

    @classmethod
    def from_data_dir(cls, data_dir: str | Path) -> LocalNotificationStore:
        return cls(data_root(data_dir) / "notifications.db")

    def _connect(self) -> sqlite3.Connection:
        return connect_kv(self._db_path)

    def add(self, source: str, notice: ExtensionNotice) -> FloorNotification | None:
        """Keep ``notice`` from ``source``. ``None`` when its event was already kept."""
        notification_id = f"{self.ID_PREFIX}{uuid.uuid4().hex}"
        created_at = datetime.now(UTC)
        with contextlib.closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO local_notifications "
                "(id, source, kind, title, body, target, event_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notification_id,
                    source,
                    notice.kind,
                    notice.title,
                    notice.body,
                    notice.target.model_dump_json(),
                    notice.event_key,
                    created_at.isoformat(),
                ),
            )
            if cursor.rowcount == 0:
                return None
            conn.execute(
                "DELETE FROM local_notifications WHERE source = ? AND id NOT IN ("
                "SELECT id FROM local_notifications WHERE source = ? "
                "ORDER BY created_at DESC LIMIT ?)",
                (source, source, self.KEEP),
            )
        return FloorNotification(
            id=notification_id,
            origin=NotificationOrigin.LOCAL,
            source=source,
            kind=notice.kind,
            title=notice.title,
            body=notice.body,
            target=notice.target,
            created_at=created_at,
            read=False,
        )

    def listing(self, *, limit: int = 100) -> list[FloorNotification]:
        """The newest ``limit``, newest first."""
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM local_notifications ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def counts(self) -> NotificationCounts:
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) AS n FROM local_notifications "
                "WHERE read_at IS NULL GROUP BY source"
            ).fetchall()
        by_source = {row["source"]: int(row["n"]) for row in rows}
        return NotificationCounts(unread=sum(by_source.values()), by_source=by_source)

    def owns(self, notification_id: str) -> bool:
        """Whether ``notification_id`` is one of this store's."""
        return notification_id.startswith(self.ID_PREFIX)

    def mark_read(self, *, ids: list[str] | None = None, source: str | None = None) -> int:
        """Mark these ids, or everything from ``source``, or (neither) everything."""
        now = datetime.now(UTC).isoformat()
        query = "UPDATE local_notifications SET read_at = ? WHERE read_at IS NULL"
        params: list[str] = [now]
        if ids is not None:
            mine = [i for i in ids if self.owns(i)]
            if not mine:
                return 0
            query += f" AND id IN ({', '.join('?' for _ in mine)})"
            params.extend(mine)
        elif source is not None:
            query += " AND source = ?"
            params.append(source)
        with contextlib.closing(self._connect()) as conn, conn:
            return conn.execute(query, params).rowcount

    @staticmethod
    def _row(row: sqlite3.Row) -> FloorNotification:
        return FloorNotification(
            id=row["id"],
            origin=NotificationOrigin.LOCAL,
            source=row["source"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            target=NotificationTarget.model_validate_json(row["target"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            read=row["read_at"] is not None,
        )
