"""Which chat a service's thread is: the durable map behind ``thread="claim:CL-20431"``.

``docs/CLOUD.md`` §6.1. A service names a conversation by a key of its
own; igni keeps, in the producer's database and scoped like every other
row (organisation, producer, project), which session that key opened
for which extension. The first item of a thread opens the chat, every
later one finds it here. The empty thread is the extension's own chat:
one per extension, where an item without a thread goes.

Also here: posts held for a chat whose run is going (:class:`HeldPosts`)
— an item with attachments waits for the run to end rather than being
queued into it, so its images and documents reach the model as a
person's upload would.
"""

from __future__ import annotations

import time

from pydantic import BaseModel
from sqlalchemy import BigInteger, PrimaryKeyConstraint, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.scope import ScopedColumns


class ServiceThreadModel(ScopedColumns, Base):
    __tablename__ = "service_threads"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "extension_id", "thread", name="pk_service_threads"
        ),
    )

    extension_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The service's key; empty for the extension's own chat.
    thread: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    #: Unix milliseconds.
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ThreadChat(BaseModel):
    extension_id: str
    thread: str
    session_id: str
    title: str = ""


class ServiceThreads:
    """The map from (extension, thread) to a session, in the producer's database."""

    #: The thread of the extension's own chat.
    OWN = ""

    def __init__(self, db: Database) -> None:
        self._db = db

    def find(self, extension_id: str, thread: str) -> ThreadChat | None:
        with self._db.sync_session() as session:
            row = session.execute(
                select(ServiceThreadModel).where(
                    self._db.scope.where(ServiceThreadModel),
                    ServiceThreadModel.extension_id == extension_id,
                    ServiceThreadModel.thread == thread,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return ThreadChat(
                extension_id=row.extension_id,
                thread=row.thread,
                session_id=row.session_id,
                title=row.title,
            )

    def bind(self, chat: ThreadChat) -> None:
        with self._db.sync_session() as session, session.begin():
            session.add(
                ServiceThreadModel(
                    **self._db.scope.stamp(),
                    extension_id=chat.extension_id,
                    thread=chat.thread,
                    session_id=chat.session_id,
                    title=chat.title,
                    created_at=int(time.time() * 1000),
                )
            )

    def retitle(self, extension_id: str, thread: str, title: str) -> None:
        with self._db.sync_session() as session, session.begin():
            row = session.execute(
                select(ServiceThreadModel).where(
                    self._db.scope.where(ServiceThreadModel),
                    ServiceThreadModel.extension_id == extension_id,
                    ServiceThreadModel.thread == thread,
                )
            ).scalar_one_or_none()
            if row is not None:
                row.title = title


class HeldPostModel(ScopedColumns, Base):
    __tablename__ = "held_service_posts"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "event_id", name="pk_held_service_posts"
        ),
    )

    event_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    extension_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The service that sent it.
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Unix milliseconds.
    held_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class HeldPost(BaseModel):
    event_id: str
    session_id: str
    extension_id: str
    client_id: str
    text: str


class HeldPosts:
    """Posts waiting for their chat's run to end, kept across restarts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def hold(self, post: HeldPost) -> None:
        with self._db.sync_session() as session, session.begin():
            exists = session.execute(
                select(HeldPostModel.event_id).where(
                    self._db.scope.where(HeldPostModel), HeldPostModel.event_id == post.event_id
                )
            ).first()
            if exists is None:
                session.add(
                    HeldPostModel(
                        **self._db.scope.stamp(),
                        **post.model_dump(),
                        held_at=int(time.time() * 1000),
                    )
                )

    def waiting(self) -> list[HeldPost]:
        """Oldest first."""
        with self._db.sync_session() as session:
            rows = session.execute(
                select(HeldPostModel)
                .where(self._db.scope.where(HeldPostModel))
                .order_by(HeldPostModel.held_at)
            ).scalars()
            return [
                HeldPost(
                    event_id=row.event_id,
                    session_id=row.session_id,
                    extension_id=row.extension_id,
                    client_id=row.client_id,
                    text=row.text,
                )
                for row in rows
            ]

    def release(self, event_id: str) -> None:
        with self._db.sync_session() as session, session.begin():
            session.execute(
                delete(HeldPostModel).where(
                    self._db.scope.where(HeldPostModel), HeldPostModel.event_id == event_id
                )
            )


__all__ = [
    "HeldPost",
    "HeldPostModel",
    "HeldPosts",
    "ServiceThreadModel",
    "ServiceThreads",
    "ThreadChat",
]
