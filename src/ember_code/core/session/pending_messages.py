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
import contextlib
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

from ember_code.core.session._sqlite_utils import connect_kv

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

# Three ``ALTER TABLE`` statements that bring an older DB up to the
# explicit-interrupted-run schema. Each is idempotent — SQLite raises
# ``OperationalError: duplicate column name`` when the column already
# exists; we swallow that error so re-running the migration is a no-op.
# See :meth:`PendingMessageStore._migrate_existing`.
_INTERRUPTED_SCHEMA_MIGRATIONS: Final[tuple[str, ...]] = (
    "ALTER TABLE ember_received_messages ADD COLUMN interrupted_at INTEGER",
    "ALTER TABLE ember_received_messages ADD COLUMN interrupted_reason TEXT",
    "ALTER TABLE ember_received_messages ADD COLUMN last_error TEXT",
)

# Wire / storage literals for the ``interrupted_reason`` column.
# Kept narrow on purpose — the FE renders these as banner labels
# (see ``AssistantInterrupted`` in ``clients/web/src/chat/model.ts``).
InterruptedReason = Literal["cancelled", "errored", "abandoned"]
INTERRUPTED_REASONS: Final[frozenset[str]] = frozenset({"cancelled", "errored", "abandoned"})


@dataclass
class PendingMessage:
    """A user message that started a run but didn't see it through."""

    message_id: str
    session_id: str
    text: str
    received_at: int  # unix seconds


@dataclass
class InterruptedMessage:
    """A pending user message whose run was cut off before completion.

    ``interrupted_at`` and ``interrupted_reason`` are populated by
    :meth:`PendingMessageStore.mark_interrupted`. ``last_error`` is
    set when ``interrupted_reason == 'errored'`` and ``None`` for
    clean cancellations.

    The FE surfaces this object as the "interrupted banner" on the
    assistant bubble that was in-flight when the run stopped.
    """

    message_id: str
    session_id: str
    text: str
    received_at: int
    interrupted_at: int
    interrupted_reason: InterruptedReason
    last_error: str | None


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
        # Also run the interrupted-run migration so an older DB
        # picks up the new columns on next launch — no separate
        # alembic step required.
        with contextlib.closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)
            self._migrate_existing(conn)

    def _migrate_existing(self, conn: sqlite3.Connection) -> None:
        """Bring an older DB up to the explicit-interrupted-run schema.

        SQLite's ``ALTER TABLE ... ADD COLUMN`` is not idempotent —
        re-running it raises ``duplicate column name``. We swallow
        that one error so the migration can be re-run safely on
        every startup; any other error propagates so genuine
        corruption is visible.
        """
        for stmt in _INTERRUPTED_SCHEMA_MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise

    def _connect(self) -> sqlite3.Connection:
        return connect_kv(self._db_path)

    def record_received(self, session_id: str, text: str) -> str:
        """Persist a freshly-received user message; return its id.

        The id is opaque and unique; callers pass it back to
        ``mark_completed`` once the run finishes successfully. Any
        row not marked completed by the time the process dies will
        be surfaced on the next ``--continue`` boot.
        """
        msg_id = str(uuid.uuid4())
        ts = int(datetime.now(timezone.utc).timestamp())
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO ember_received_messages "
                "(message_id, session_id, text, received_at, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (msg_id, session_id, text, ts),
            )
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
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE ember_received_messages "
                    "SET status='completed', completed_at=? "
                    "WHERE message_id=?",
                    (ts, message_id),
                )
        except Exception as exc:
            logger.debug("mark_completed failed for %s: %s", message_id, exc)

    def list_pending(self, session_id: str) -> list[PendingMessage]:
        """Return every still-pending message for the session.

        Sorted oldest first so callers can recap in submission
        order. Limited to a few rows defensively — even if the
        process crashed multiple times in succession we don't want
        to flood the next agent invocation with stale prompts.
        """
        with contextlib.closing(self._connect()) as conn:
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
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "DELETE FROM ember_received_messages WHERE message_id=?",
                    (message_id,),
                )
        except Exception as exc:
            logger.debug("discard failed for %s: %s", message_id, exc)

    def mark_interrupted(
        self,
        message_id: str,
        reason: InterruptedReason,
        last_error: str | None = None,
    ) -> None:
        """Stamp the row as an explicitly-interrupted run.

        Called from the cancel + error paths in
        :class:`RunController` so the FE has a durable record to
        recover on restart — not just an inferred dangling pending
        row + Agno ``RunStatus.running`` heuristic.

        Idempotent on ``status='completed'``: if the row was already
        marked completed (rare race: cancel arrived after natural
        completion), the ``AND status='pending'`` guard turns the
        UPDATE into a no-op so we never resurrect a completed run
        as "interrupted".

        Failure here is non-fatal: a missing write only means the
        next ``detect_interrupted_run`` falls back to the legacy
        heuristic. We log and continue so cancel/error paths don't
        crash mid-shutdown.
        """
        if reason not in INTERRUPTED_REASONS:
            raise ValueError(f"unknown interrupted_reason: {reason!r}")
        ts = int(datetime.now(timezone.utc).timestamp())
        try:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE ember_received_messages "
                    "SET interrupted_at=?, interrupted_reason=?, last_error=? "
                    "WHERE message_id=? AND status='pending'",
                    (ts, reason, last_error, message_id),
                )
        except Exception as exc:
            logger.debug("mark_interrupted failed for %s: %s", message_id, exc)

    def list_interrupted(self, session_id: str) -> list[InterruptedMessage]:
        """Return every row explicitly marked as interrupted for the session.

        A row is "interrupted" iff ``interrupted_at IS NOT NULL`` and
        ``status = 'pending'``. Used by ``get_interrupted_runs`` so the
        FE can render banners on history restore / app restart.

        Sorted oldest first; limited to 5 rows defensively (the FE
        doesn't need more than a handful of banners on screen at
        once — if a user has more than 5 interrupted runs queued,
        surfacing the most recent N is plenty).
        """
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT message_id, session_id, text, received_at, "
                "interrupted_at, interrupted_reason, last_error "
                "FROM ember_received_messages "
                "WHERE session_id=? AND status='pending' "
                "AND interrupted_at IS NOT NULL "
                "ORDER BY interrupted_at ASC "
                "LIMIT 5",
                (session_id,),
            ).fetchall()
        return [
            InterruptedMessage(
                message_id=r["message_id"],
                session_id=r["session_id"],
                text=r["text"],
                received_at=r["received_at"],
                interrupted_at=r["interrupted_at"],
                interrupted_reason=r["interrupted_reason"],
                last_error=r["last_error"],
            )
            for r in rows
        ]

    def discard_all_for_session(self, session_id: str) -> int:
        """Hard-delete every pending + interrupted row for the session.

        Used when the FE re-fires a ``user_message(force=true)`` —
        the retry semantic is "supersede any stale interrupted
        state, then start clean". Returns the number of rows
        deleted so callers can log it for debugging.

        Does NOT touch completed rows — those are real history and
        must remain queryable.
        """
        try:
            with contextlib.closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    "DELETE FROM ember_received_messages WHERE session_id=? AND status='pending'",
                    (session_id,),
                )
                return cur.rowcount
        except Exception as exc:
            logger.debug("discard_all_for_session failed for %s: %s", session_id, exc)
            return 0

    # ── Async wrappers (the hot paths) ────────────────────────────

    async def arecord_received(self, session_id: str, text: str) -> str:
        return await asyncio.to_thread(self.record_received, session_id, text)

    async def amark_completed(self, message_id: str) -> None:
        await asyncio.to_thread(self.mark_completed, message_id)

    async def alist_pending(self, session_id: str) -> list[PendingMessage]:
        return await asyncio.to_thread(self.list_pending, session_id)

    async def adiscard(self, message_id: str) -> None:
        await asyncio.to_thread(self.discard, message_id)

    async def amark_interrupted(
        self,
        message_id: str,
        reason: InterruptedReason,
        last_error: str | None = None,
    ) -> None:
        await asyncio.to_thread(self.mark_interrupted, message_id, reason, last_error)

    async def alist_interrupted(self, session_id: str) -> list[InterruptedMessage]:
        return await asyncio.to_thread(self.list_interrupted, session_id)

    async def adiscard_all_for_session(self, session_id: str) -> int:
        return await asyncio.to_thread(self.discard_all_for_session, session_id)
