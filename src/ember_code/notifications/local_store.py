"""Notifications an extension told this machine, and whether they were read.

igni server's ``docs/service-identity.md`` §10.2. "Your export is ready"
concerns the person at this keyboard and nobody else, so it never
leaves the machine: the notice and its read state are a row in igni's
global database (the desktop's ``ember.db``; in the cloud the shared
database, scoped to the producer), and nothing here talks to igni
server.

**Once per event.** An extension that retries, or restarts and says the
same thing again, names the event with ``event_key``; a second notice
with the same key from the same extension is dropped, read or not.

**Bounded.** The newest :attr:`LocalNotificationStore.KEEP` are kept per
extension. A process that notifies in a loop costs a fixed amount of
disk, not an unbounded one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Index, String, Text, UniqueConstraint, delete, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import ScopedColumns
from ember_code.core.db.upsert import ConflictInsert
from ember_code.notifications.schemas import (
    ExtensionNotice,
    FloorNotification,
    NotificationCounts,
    NotificationOrigin,
    NotificationTarget,
)


class LocalNotificationModel(ScopedColumns, Base):
    __tablename__ = "local_notifications"
    __table_args__ = (
        UniqueConstraint(
            "org", "producer", "project", "source", "event_key", name="uq_local_notifications_event"
        ),
        Index("ix_local_notifications_scope_created", "org", "producer", "project", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: :class:`NotificationTarget` as JSON.
    target: Mapped[str] = mapped_column(Text, nullable=False)
    event_key: Mapped[str] = mapped_column(String, nullable=False)
    #: ISO 8601 in UTC — sorts as it reads, and keeps its offset.
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    read_at: Mapped[str | None] = mapped_column(String, nullable=True)


class LocalNotificationStore:
    """The table local notifications live in."""

    #: Kept per extension; older ones are dropped as new ones arrive.
    KEEP = 200
    #: The prefix every local id carries, so it can never be mistaken
    #: for one igni server issued.
    ID_PREFIX = "local-"

    def __init__(self, db: Database | str | Path) -> None:
        self._db = Database.of(db)

    @classmethod
    def from_locator(cls, locator: StorageLocator) -> LocalNotificationStore:
        return cls(locator.global_database())

    def add(self, source: str, notice: ExtensionNotice) -> FloorNotification | None:
        """Keep ``notice`` from ``source``. ``None`` when its event was already kept."""
        notification_id = f"{self.ID_PREFIX}{uuid.uuid4().hex}"
        created_at = datetime.now(UTC)
        scope = self._db.scope
        with self._db.sync_session() as session, session.begin():
            inserted = session.execute(
                ConflictInsert.into(session, LocalNotificationModel)
                .values(
                    **scope.stamp(),
                    id=notification_id,
                    source=source,
                    kind=notice.kind,
                    title=notice.title,
                    body=notice.body,
                    target=notice.target.model_dump_json(),
                    event_key=notice.event_key,
                    created_at=created_at.isoformat(),
                )
                .on_conflict_do_nothing(
                    index_elements=["org", "producer", "project", "source", "event_key"]
                )
                .returning(LocalNotificationModel.id)
            ).scalar_one_or_none()
            if inserted is None:
                return None
            mine = (scope.where(LocalNotificationModel), LocalNotificationModel.source == source)
            kept = (
                select(LocalNotificationModel.id)
                .where(*mine)
                .order_by(LocalNotificationModel.created_at.desc())
                .limit(self.KEEP)
            )
            session.execute(
                delete(LocalNotificationModel).where(
                    *mine, LocalNotificationModel.id.not_in(kept.scalar_subquery())
                )
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
        with self._db.sync_session() as session:
            rows = session.scalars(
                select(LocalNotificationModel)
                .where(self._db.scope.where(LocalNotificationModel))
                .order_by(LocalNotificationModel.created_at.desc())
                .limit(limit)
            ).all()
        return [self._row(row) for row in rows]

    def counts(self) -> NotificationCounts:
        with self._db.sync_session() as session:
            rows = session.execute(
                select(LocalNotificationModel.source, func.count())
                .where(
                    self._db.scope.where(LocalNotificationModel),
                    LocalNotificationModel.read_at.is_(None),
                )
                .group_by(LocalNotificationModel.source)
            ).all()
        by_source = {source: int(n) for source, n in rows}
        return NotificationCounts(unread=sum(by_source.values()), by_source=by_source)

    def owns(self, notification_id: str) -> bool:
        """Whether ``notification_id`` is one of this store's."""
        return notification_id.startswith(self.ID_PREFIX)

    def mark_read(self, *, ids: list[str] | None = None, source: str | None = None) -> int:
        """Mark these ids, or everything from ``source``, or (neither) everything."""
        conditions = [
            self._db.scope.where(LocalNotificationModel),
            LocalNotificationModel.read_at.is_(None),
        ]
        if ids is not None:
            mine = [i for i in ids if self.owns(i)]
            if not mine:
                return 0
            conditions.append(LocalNotificationModel.id.in_(mine))
        elif source is not None:
            conditions.append(LocalNotificationModel.source == source)
        with self._db.sync_session() as session, session.begin():
            result = session.execute(
                update(LocalNotificationModel)
                .where(*conditions)
                .values(read_at=datetime.now(UTC).isoformat())
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    @staticmethod
    def _row(row: LocalNotificationModel) -> FloorNotification:
        return FloorNotification(
            id=row.id,
            origin=NotificationOrigin.LOCAL,
            source=row.source,
            kind=row.kind,
            title=row.title,
            body=row.body,
            target=NotificationTarget.model_validate_json(row.target),
            created_at=datetime.fromisoformat(row.created_at),
            read=row.read_at is not None,
        )
