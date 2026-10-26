"""Messages typed during a run, kept until the run takes them.

A person (or an extension, ``host.chat.queue``) can send a message while
the agent is working; it waits in a queue the run drains after its next
tool call (``core/hooks/queue``). That queue was a plain list in the
backend's memory, so a restart lost what was waiting in it. In the cloud
a producer's VM can be lost at any time (``docs/CLOUD.md`` section 9).

:class:`DurableMessageQueue` is the same queue, written through to the
project's database: a message is a row from the moment it is queued
until the moment a run takes it, and taking it deletes the row in the
same transaction that reads it. A backend that starts again for the
session finds what was still waiting, and nothing it already gave a run:
each message is delivered once, never twice. One taken by a run that
then died is gone with that run, as the rest of the run is.

It is a ``MutableSequence[str]`` because the queue's users treat it as a
list — ``append``, ``pop(0)``, ``while queue`` — and keeps the rows'
order in memory, so reading it costs nothing.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable, MutableSequence
from pathlib import Path
from typing import overload

from sqlalchemy import BigInteger, Index, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.scope import ScopedColumns

logger = logging.getLogger(__name__)


class QueuedMessageModel(ScopedColumns, Base):
    __tablename__ = "queued_messages"
    __table_args__ = (
        Index("ix_queued_messages_session", "org", "producer", "project", "session_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    #: Order within the session: nanoseconds when queued.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class QueuedMessageStore:
    """The ``queued_messages`` table of one project's database."""

    def __init__(self, db: Database | str | Path) -> None:
        self._db = Database.of(db)

    def add(self, session_id: str, text: str, *, seq: int | None = None) -> str:
        message_id = uuid.uuid4().hex
        with self._db.sync_session() as session, session.begin():
            session.add(
                QueuedMessageModel(
                    **self._db.scope.stamp(),
                    id=message_id,
                    session_id=session_id,
                    seq=seq if seq is not None else time.time_ns(),
                    text=text,
                )
            )
        return message_id

    def take(self, message_id: str) -> bool:
        """Delete the row; ``True`` when it was still there to take."""
        with self._db.sync_session() as session, session.begin():
            result = session.execute(
                delete(QueuedMessageModel).where(
                    self._db.scope.where(QueuedMessageModel), QueuedMessageModel.id == message_id
                )
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    def waiting(self, session_id: str) -> list[tuple[str, str]]:
        """``(id, text)`` of the session's queued messages, in order."""
        with self._db.sync_session() as session:
            rows = session.execute(
                select(QueuedMessageModel.id, QueuedMessageModel.text)
                .where(
                    self._db.scope.where(QueuedMessageModel),
                    QueuedMessageModel.session_id == session_id,
                )
                .order_by(QueuedMessageModel.seq, QueuedMessageModel.id)
            ).all()
        return [(message_id, text) for message_id, text in rows]


class DurableMessageQueue(MutableSequence[str]):
    """A session's queue of waiting messages, each one a row until taken.

    ``session_id`` is asked on every write: the backend's session id can
    change under it (a new chat in the same window).
    """

    def __init__(self, store: QueuedMessageStore, session_id: Callable[[], str]) -> None:
        self._store = store
        self._session_id = session_id
        #: ``(row id, text)`` in queue order; ``""`` for a row that could
        #: not be written (it still waits, in memory).
        self._items: list[tuple[str, str]] = []
        self.restore()

    def restore(self) -> None:
        """Read back what the current session left waiting."""
        try:
            self._items = self._store.waiting(self._session_id())
        except Exception as exc:
            logger.debug("queued messages: restore failed: %s", exc)
            self._items = []

    def _write(self, text: str) -> str:
        try:
            return self._store.add(self._session_id(), text)
        except Exception as exc:
            logger.debug("queued messages: write failed: %s", exc)
            return ""

    def _take(self, message_id: str) -> None:
        if not message_id:
            return
        try:
            self._store.take(message_id)
        except Exception as exc:
            logger.debug("queued messages: take %s failed: %s", message_id, exc)

    # ── MutableSequence ───────────────────────────────────────────

    @overload
    def __getitem__(self, index: int) -> str: ...
    @overload
    def __getitem__(self, index: slice) -> list[str]: ...
    def __getitem__(self, index: int | slice) -> str | list[str]:
        if isinstance(index, slice):
            return [text for _, text in self._items[index]]
        return self._items[index][1]

    def __setitem__(self, index: int | slice, value: str | Iterable[str]) -> None:  # type: ignore[override]
        raise TypeError("a queued message is taken or kept, never rewritten")

    def __delitem__(self, index: int | slice) -> None:
        taken = self._items[index] if isinstance(index, slice) else [self._items[index]]
        del self._items[index]
        for message_id, _ in taken:
            self._take(message_id)

    def __len__(self) -> int:
        return len(self._items)

    def insert(self, index: int, value: str) -> None:
        # Appending is the only order a row can be written in; a message
        # put back at the front (none of the queue's users do) keeps its
        # place in memory and goes to the end in the database.
        self._items.insert(index, (self._write(value), value))

    def append(self, value: str) -> None:
        self._items.append((self._write(value), value))

    def pop(self, index: int = -1) -> str:
        message_id, text = self._items.pop(index)
        self._take(message_id)
        return text

    def clear(self) -> None:
        taken, self._items = self._items, []
        for message_id, _ in taken:
            self._take(message_id)

    def __repr__(self) -> str:
        return f"DurableMessageQueue({[text for _, text in self._items]!r})"


__all__ = ["DurableMessageQueue", "QueuedMessageModel", "QueuedMessageStore"]
