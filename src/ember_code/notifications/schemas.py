"""The shapes notifications take, from igni server, from an extension, and to the client.

igni server's ``docs/service-identity.md`` §10. Two paths arrive here
and leave as one: a row igni server delivered to this person
(:class:`ServerNotification`), and a notice an extension's own process
told this host (:class:`ExtensionNotice`). Both become a
:class:`FloorNotification`, which is all the client ever sees.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NotificationTarget(BaseModel):
    """Where opening a notification goes: one of its extension's pages."""

    model_config = ConfigDict(extra="ignore")

    page: str = Field(min_length=1)
    #: The page's launch parameters. Strings, because they end up in a
    #: route the client persists and in the extension's ``params``; a
    #: number or a flag is written as its JSON text rather than dropped.
    params: dict[str, str] = Field(default_factory=dict)

    @field_validator("params", mode="before")
    @classmethod
    def _as_strings(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            str(key): item if isinstance(item, str) else json.dumps(item)
            for key, item in value.items()
            if item is not None
        }


class ServerNotification(BaseModel):
    """One row of ``GET /v1/notifications`` (§10.1)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    #: The extension the notifying service ships, recorded by igni server.
    source: str
    kind: str = ""
    title: str = ""
    body: str = ""
    actor: str | None = None
    target: NotificationTarget | None = None
    created_at: datetime | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None


class ServerNotificationPage(BaseModel):
    """What ``GET /v1/notifications`` answers."""

    model_config = ConfigDict(extra="ignore")

    notifications: list[ServerNotification] = Field(default_factory=list)


class NotificationCounts(BaseModel):
    """Unread, in all and per source — the badge on each extension."""

    model_config = ConfigDict(extra="ignore")

    unread: int = 0
    by_source: dict[str, int] = Field(default_factory=dict)

    def plus(self, other: NotificationCounts) -> NotificationCounts:
        """Both counts, added source by source."""
        by_source = dict(self.by_source)
        for source, count in other.by_source.items():
            by_source[source] = by_source.get(source, 0) + count
        return NotificationCounts(
            unread=self.unread + other.unread,
            by_source={source: n for source, n in by_source.items() if n > 0},
        )


class MarkReadRequest(BaseModel):
    """``POST /v1/notifications/read``: these ids, this source, or (neither) all."""

    ids: list[str] | None = None
    source: str | None = None

    def body(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ExtensionNotice(BaseModel):
    """The params of ``notifications/igni/notify`` (§10.2).

    §10.1's shape without ``recipients`` or ``actor``: the person is
    whoever is using this igni. Bounded, because it is drawn in igni's
    own chrome and a process can send anything.
    """

    model_config = ConfigDict(extra="ignore")

    kind: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=1000)
    target: NotificationTarget
    event_key: str = Field(min_length=1, max_length=200)


class NotificationOrigin(StrEnum):
    """Which of the two paths a notification came by."""

    #: Delivered by igni server; its read state lives there.
    SERVER = "server"
    #: Told by an extension on this machine; its read state lives here.
    LOCAL = "local"


class FloorNotification(BaseModel):
    """One notification, as the client draws it."""

    id: str
    origin: NotificationOrigin
    #: The extension it is from.
    source: str
    kind: str = ""
    title: str = ""
    body: str = ""
    actor: str = ""
    target: NotificationTarget | None = None
    created_at: datetime | None = None
    read: bool = False

    @classmethod
    def from_server(cls, row: ServerNotification) -> FloorNotification:
        return cls(
            id=row.id,
            origin=NotificationOrigin.SERVER,
            source=row.source,
            kind=row.kind,
            title=row.title,
            body=row.body,
            actor=row.actor or "",
            target=row.target,
            created_at=row.created_at,
            read=row.read_at is not None,
        )


class NotificationsListResult(BaseModel):
    """``notifications_list``: both paths, newest first, and the counts."""

    notifications: list[FloorNotification] = Field(default_factory=list)
    counts: NotificationCounts = Field(default_factory=NotificationCounts)
    #: Why igni server's half is not current, when it is not. The local
    #: half and the last server rows that were read are still returned:
    #: "could not ask" is not "nothing is waiting".
    server_error: str = ""


class NotificationsReadResult(BaseModel):
    """``notifications_mark_read``: the counts after marking."""

    counts: NotificationCounts = Field(default_factory=NotificationCounts)
    server_error: str = ""


class NotificationsChanged(BaseModel):
    """The ``notifications_changed`` push: the counts moved."""

    counts: NotificationCounts = Field(default_factory=NotificationCounts)
