"""Messages typed during a run, kept until the conversation holds them.

A person (or an extension, ``host.chat.queue``) can send a message while
the agent is working; it waits in a queue the run drains after its next
tool call (``core/hooks/queue``). That queue was a plain list in the
backend's memory, so a restart lost what was waiting in it. In the cloud
a producer's VM can be lost at any time (``docs/CLOUD.md`` section 9).

:class:`DurableMessageQueue` is the same queue, written through to the
project's database, one row per message:

* **waiting** from the moment it is queued;
* **taken** when a run drains it (:meth:`DurableMessageQueue.take`) —
  the row stays, and the message is recorded in the conversation under
  the row's id (the queue persister's ``Message.id``);
* **deleted** once the conversation holds it: after a run
  (:meth:`DurableMessageQueue.settle`), and when a backend starts
  (:meth:`DurableMessageQueue.recover`), which also puts every taken
  message the conversation does *not* hold back in the queue.

So a message is delivered into the conversation exactly once: a crash
after it was taken but before the session was saved offers it again,
and a crash after the session was saved finds it there and drops it.

It is a ``MutableSequence[str]`` because the queue's users treat it as a
list — ``append``, ``pop(0)``, ``while queue`` — and keeps the waiting
rows' order in memory, so reading it costs nothing.
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from collections.abc import Callable, Iterable, MutableSequence
from enum import StrEnum
from pathlib import Path
from typing import Any, overload

from agno.db.base import SessionType
from sqlalchemy import BigInteger, Index, String, Text, delete, select, update
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.scope import ScopedColumns
from ember_code.core.hooks.queue.schemas import TakenMessage

logger = logging.getLogger(__name__)


class QueuedStatus(StrEnum):
    WAITING = "waiting"
    #: A run drained it; the conversation may not hold it yet.
    TAKEN = "taken"


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
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="waiting")


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
                    status=QueuedStatus.WAITING.value,
                )
            )
        return message_id

    def _set_status(self, message_ids: Iterable[str], status: QueuedStatus) -> None:
        ids = list(message_ids)
        if not ids:
            return
        with self._db.sync_session() as session, session.begin():
            session.execute(
                update(QueuedMessageModel)
                .where(self._db.scope.where(QueuedMessageModel), QueuedMessageModel.id.in_(ids))
                .values(status=status.value)
            )

    def mark_taken(self, message_id: str) -> None:
        self._set_status([message_id], QueuedStatus.TAKEN)

    def mark_waiting(self, message_ids: Iterable[str]) -> None:
        self._set_status(message_ids, QueuedStatus.WAITING)

    def remove(self, message_ids: Iterable[str]) -> None:
        ids = list(message_ids)
        if not ids:
            return
        with self._db.sync_session() as session, session.begin():
            session.execute(
                delete(QueuedMessageModel).where(
                    self._db.scope.where(QueuedMessageModel), QueuedMessageModel.id.in_(ids)
                )
            )

    def rows(self, session_id: str, status: QueuedStatus) -> list[tuple[str, str]]:
        """``(id, text)`` of the session's messages in ``status``, in order."""
        with self._db.sync_session() as session:
            rows = session.execute(
                select(QueuedMessageModel.id, QueuedMessageModel.text)
                .where(
                    self._db.scope.where(QueuedMessageModel),
                    QueuedMessageModel.session_id == session_id,
                    QueuedMessageModel.status == status.value,
                )
                .order_by(QueuedMessageModel.seq, QueuedMessageModel.id)
            ).all()
        return [(message_id, text) for message_id, text in rows]


class RecordedMessages:
    """Which message ids a session's conversation holds, from Agno's copy."""

    @staticmethod
    async def ids(agno_db: Any, session_id: str) -> set[str]:
        found = agno_db.get_session(session_id=session_id, session_type=SessionType.AGENT)
        if inspect.isawaitable(found):
            found = await found
        ids: set[str] = set()
        for run in getattr(found, "runs", None) or []:
            for message in getattr(run, "messages", None) or []:
                message_id = getattr(message, "id", None)
                if message_id:
                    ids.add(str(message_id))
        return ids


class DurableMessageQueue(MutableSequence[str]):
    """A session's queue of waiting messages, each one a row until recorded.

    ``session_id`` is asked on every write: the backend's session id can
    change under it (a new chat in the same window).
    """

    def __init__(self, store: QueuedMessageStore, session_id: Callable[[], str]) -> None:
        self._store = store
        self._session_id = session_id
        #: ``(row id, text)`` of the waiting messages, in queue order;
        #: ``""`` for a row that could not be written (it still waits,
        #: in memory).
        self._items: list[tuple[str, str]] = []
        self._load()

    def _load(self) -> None:
        try:
            self._items = self._store.rows(self._session_id(), QueuedStatus.WAITING)
        except Exception as exc:
            logger.debug("queued messages: load failed: %s", exc)
            self._items = []

    # ── The lifecycle beyond a list ───────────────────────────────

    def take(self) -> TakenMessage:
        """The next message, for a run: recorded as taken, not deleted."""
        message_id, text = self._items.pop(0)
        if message_id:
            try:
                self._store.mark_taken(message_id)
            except Exception as exc:
                logger.debug("queued messages: take %s failed: %s", message_id, exc)
        return TakenMessage(id=message_id, text=text)

    async def settle(self, agno_db: Any) -> None:
        """Delete the taken messages the conversation now holds.

        After a run. Never puts a message back: a run may still be about
        to record one (a paused run, say) — only :meth:`recover`, when no
        run can be holding any, does that.
        """
        try:
            session_id = self._session_id()
            taken = self._store.rows(session_id, QueuedStatus.TAKEN)
            if not taken:
                return
            recorded = await RecordedMessages.ids(agno_db, session_id)
            self._store.remove(mid for mid, _ in taken if mid in recorded)
        except Exception as exc:
            logger.debug("queued messages: settle failed: %s", exc)

    async def recover(self, agno_db: Any) -> int:
        """At start: drop what the conversation holds, offer the rest again.

        A taken message the session does not hold was drained by a run
        the previous backend did not finish saving. It goes back to the
        front of the queue, in its order. Returns how many came back.
        """
        try:
            session_id = self._session_id()
            taken = self._store.rows(session_id, QueuedStatus.TAKEN)
            if not taken:
                return 0
            recorded = await RecordedMessages.ids(agno_db, session_id)
            self._store.remove(mid for mid, _ in taken if mid in recorded)
            again = [(mid, text) for mid, text in taken if mid not in recorded]
            self._store.mark_waiting(mid for mid, _ in again)
        except Exception as exc:
            logger.debug("queued messages: recover failed: %s", exc)
            return 0
        self._load()
        return len(again)

    def _write(self, text: str) -> str:
        try:
            return self._store.add(self._session_id(), text)
        except Exception as exc:
            logger.debug("queued messages: write failed: %s", exc)
            return ""

    def _drop(self, message_id: str) -> None:
        if not message_id:
            return
        try:
            self._store.remove([message_id])
        except Exception as exc:
            logger.debug("queued messages: drop %s failed: %s", message_id, exc)

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
        dropped = self._items[index] if isinstance(index, slice) else [self._items[index]]
        del self._items[index]
        for message_id, _ in dropped:
            self._drop(message_id)

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
        """Take a message (``pop(0)``, as the queue's users call it).

        Taken, not deleted — see :meth:`take`. Any other index removes
        the message without delivering it.
        """
        if index == 0 or index == -len(self._items):
            return self.take().text
        message_id, text = self._items.pop(index)
        self._drop(message_id)
        return text

    def clear(self) -> None:
        dropped, self._items = self._items, []
        for message_id, _ in dropped:
            self._drop(message_id)

    def __repr__(self) -> str:
        return f"DurableMessageQueue({[text for _, text in self._items]!r})"


__all__ = [
    "DurableMessageQueue",
    "QueuedMessageModel",
    "QueuedMessageStore",
    "QueuedStatus",
    "RecordedMessages",
]
