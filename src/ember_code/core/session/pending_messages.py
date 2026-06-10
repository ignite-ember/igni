"""Durable user-message log — survives mid-run crashes.

Agno persists session state only at end-of-run via
``asave_session``. During the run nothing is written to disk, so a
process crash mid-stream loses everything — the user's prompt, the
partial assistant response, and any tool work in flight. Phase 2's
incremental ``_checkpoint_session`` calls help when the run has
tool boundaries to hang saves off, but a pure text-only response
(no tools) has NO event Agno fires that maps to a meaningful disk
write.

This module fills that gap with a tiny separate table managed by us:

* ``run_message`` writes a ``pending`` row before calling
  ``team.arun`` — so the user's prompt is on disk before any
  modelside work begins.
* On successful return the row is marked ``completed``.
* On crash / kill / network drop, the row stays ``pending`` and
  the next ``--continue`` boot surfaces it to the agent.

The table lives in the same project-local ``state.db`` Agno uses,
so no new file or migration system is needed. ``CREATE TABLE IF
NOT EXISTS`` runs at first use; existing databases pick up the
table on next launch.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# Schema kept simple on purpose — single table, no joins, no
# foreign keys. The session_id matches whatever Agno uses so callers
# can correlate without an extra lookup.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ember_received_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    text TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ember_received_messages_session_status
    ON ember_received_messages(session_id, status);
"""


@dataclass
class PendingMessage:
    """A user message that started a run but didn't see it through."""

    message_id: str
    session_id: str
    text: str
    received_at: int  # unix seconds


class PendingMessageStore:
    """SQLite-backed log of in-flight user messages.

    Methods are sync — SQLite writes are local and small (one row,
    well under a millisecond). They're invoked from async code via
    ``asyncio.to_thread`` so the event loop stays free for the
    streaming work.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Create the table eagerly so the first write doesn't race
        # multiple call sites trying to create it simultaneously.
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # ``isolation_level=None`` plus explicit commits keeps the
        # auto-commit behaviour predictable across concurrent runs.
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def record_received(self, session_id: str, text: str) -> str:
        """Persist a freshly-received user message; return its id.

        The id is opaque and unique; callers pass it back to
        ``mark_completed`` once the run finishes successfully. Any
        row not marked completed by the time the process dies will
        be surfaced on the next ``--continue`` boot.
        """
        msg_id = str(uuid.uuid4())
        ts = int(datetime.now(timezone.utc).timestamp())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ember_received_messages "
                "(message_id, session_id, text, received_at, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (msg_id, session_id, text, ts),
            )
            conn.commit()
        return msg_id

    def mark_completed(self, message_id: str) -> None:
        """Flip the pending row to completed.

        Called from the ``run_message`` success path. Failure here
        is non-fatal: a stale ``pending`` row will just trigger a
        spurious "interrupted previous run" nudge on the next boot,
        which is a much better failure mode than crashing the run
        that just completed successfully.
        """
        ts = int(datetime.now(timezone.utc).timestamp())
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE ember_received_messages "
                    "SET status='completed', completed_at=? "
                    "WHERE message_id=?",
                    (ts, message_id),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("mark_completed failed for %s: %s", message_id, exc)

    def list_pending(self, session_id: str) -> list[PendingMessage]:
        """Return every still-pending message for the session.

        Sorted oldest first so callers can recap in submission
        order. Limited to a few rows defensively — even if the
        process crashed multiple times in succession we don't want
        to flood the next agent invocation with stale prompts.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT message_id, session_id, text, received_at "
                "FROM ember_received_messages "
                "WHERE session_id=? AND status='pending' "
                "ORDER BY received_at ASC "
                "LIMIT 5",
                (session_id,),
            ).fetchall()
        return [
            PendingMessage(
                message_id=r["message_id"],
                session_id=r["session_id"],
                text=r["text"],
                received_at=r["received_at"],
            )
            for r in rows
        ]

    def discard(self, message_id: str) -> None:
        """Hard-delete a pending row.

        Used by the resume flow after the agent has acknowledged
        the interrupted message — we don't want it surfacing
        again on the next boot too.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM ember_received_messages WHERE message_id=?",
                    (message_id,),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("discard failed for %s: %s", message_id, exc)

    # ── Async wrappers (the hot paths) ────────────────────────────

    async def arecord_received(self, session_id: str, text: str) -> str:
        return await asyncio.to_thread(self.record_received, session_id, text)

    async def amark_completed(self, message_id: str) -> None:
        await asyncio.to_thread(self.mark_completed, message_id)

    async def alist_pending(self, session_id: str) -> list[PendingMessage]:
        return await asyncio.to_thread(self.list_pending, session_id)

    async def adiscard(self, message_id: str) -> None:
        await asyncio.to_thread(self.discard, message_id)
