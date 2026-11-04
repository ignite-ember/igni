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

The table lives in the project's database (the desktop's ``state.db``,
the same file Agno uses) and is created by the alembic migrations like
every other igni table; every query is filtered by the database's
:class:`~ember_code.core.db.scope.StorageScope`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

from sqlalchemy import ColumnElement, Index, Integer, String, Text, delete, select, update
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.scope import ScopedColumns

logger = logging.getLogger(__name__)


class ReceivedMessageModel(ScopedColumns, Base):
    """One user message, from receipt until its run completes.

    Single table, no joins, no foreign keys. ``session_id`` matches
    whatever Agno uses so callers can correlate without a lookup.
    """

    __tablename__ = "ember_received_messages"
    __table_args__ = (
        Index(
            "ix_ember_received_messages_session_status",
            "org",
            "producer",
            "project",
            "session_id",
            "status",
        ),
    )

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    completed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interrupted_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interrupted_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    """Log of in-flight user messages, in the project's database.

    Methods are sync — the writes are small (one row). They're invoked
    from async code via ``asyncio.to_thread`` so the event loop stays
    free for the streaming work.
    """

    #: Rows returned by the list queries — even if the process crashed
    #: several times in a row, the next run is not flooded with stale
    #: prompts.
    LIST_LIMIT = 5

    def __init__(self, db: Database | str | Path) -> None:
        self._db = Database.of(db)

    @staticmethod
    def _now() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def _mine(self) -> ColumnElement[bool]:
        return self._db.scope.where(ReceivedMessageModel)

    def record_received(self, session_id: str, text: str) -> str:
        """Persist a freshly-received user message; return its id.

        The id is opaque and unique; callers pass it back to
        ``mark_completed`` once the run finishes successfully. Any
        row not marked completed by the time the process dies will
        be surfaced on the next ``--continue`` boot.
        """
        msg_id = str(uuid.uuid4())
        with self._db.sync_session() as session, session.begin():
            session.add(
                ReceivedMessageModel(
                    **self._db.scope.stamp(),
                    message_id=msg_id,
                    session_id=session_id,
                    text=text,
                    received_at=self._now(),
                    status="pending",
                )
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
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    update(ReceivedMessageModel)
                    .where(self._mine(), ReceivedMessageModel.message_id == message_id)
                    .values(status="completed", completed_at=self._now())
                )
        except Exception as exc:
            logger.debug("mark_completed failed for %s: %s", message_id, exc)

    def list_pending(self, session_id: str) -> list[PendingMessage]:
        """Return every still-pending message for the session.

        Sorted oldest first so callers can recap in submission order,
        at most :attr:`LIST_LIMIT` of them.
        """
        with self._db.sync_session() as session:
            rows = session.scalars(
                select(ReceivedMessageModel)
                .where(
                    self._mine(),
                    ReceivedMessageModel.session_id == session_id,
                    ReceivedMessageModel.status == "pending",
                )
                .order_by(ReceivedMessageModel.received_at.asc())
                .limit(self.LIST_LIMIT)
            ).all()
        return [
            PendingMessage(
                message_id=r.message_id,
                session_id=r.session_id,
                text=r.text,
                received_at=r.received_at,
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
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    delete(ReceivedMessageModel).where(
                        self._mine(), ReceivedMessageModel.message_id == message_id
                    )
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
        completion), the ``status='pending'`` guard turns the
        UPDATE into a no-op so we never resurrect a completed run
        as "interrupted".

        Failure here is non-fatal: a missing write only means the
        next ``detect_interrupted_run`` falls back to the legacy
        heuristic. We log and continue so cancel/error paths don't
        crash mid-shutdown.
        """
        if reason not in INTERRUPTED_REASONS:
            raise ValueError(f"unknown interrupted_reason: {reason!r}")
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    update(ReceivedMessageModel)
                    .where(
                        self._mine(),
                        ReceivedMessageModel.message_id == message_id,
                        ReceivedMessageModel.status == "pending",
                    )
                    .values(
                        interrupted_at=self._now(),
                        interrupted_reason=reason,
                        last_error=last_error,
                    )
                )
        except Exception as exc:
            logger.debug("mark_interrupted failed for %s: %s", message_id, exc)

    def list_interrupted(self, session_id: str) -> list[InterruptedMessage]:
        """Return every row explicitly marked as interrupted for the session.

        A row is "interrupted" iff ``interrupted_at IS NOT NULL`` and
        ``status = 'pending'``. Used by ``get_interrupted_runs`` so the
        FE can render banners on history restore / app restart.

        Sorted oldest first; at most :attr:`LIST_LIMIT` rows (the FE
        doesn't need more than a handful of banners on screen at once).
        """
        with self._db.sync_session() as session:
            rows = session.scalars(
                select(ReceivedMessageModel)
                .where(
                    self._mine(),
                    ReceivedMessageModel.session_id == session_id,
                    ReceivedMessageModel.status == "pending",
                    ReceivedMessageModel.interrupted_at.is_not(None),
                )
                .order_by(ReceivedMessageModel.interrupted_at.asc())
                .limit(self.LIST_LIMIT)
            ).all()
        return [
            InterruptedMessage(
                message_id=r.message_id,
                session_id=r.session_id,
                text=r.text,
                received_at=r.received_at,
                interrupted_at=r.interrupted_at or 0,
                interrupted_reason=r.interrupted_reason,  # type: ignore[arg-type]
                last_error=r.last_error,
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
            with self._db.sync_session() as session, session.begin():
                result = session.execute(
                    delete(ReceivedMessageModel).where(
                        self._mine(),
                        ReceivedMessageModel.session_id == session_id,
                        ReceivedMessageModel.status == "pending",
                    )
                )
                return int(result.rowcount or 0)  # type: ignore[attr-defined]
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
