"""The wire behind `Agent`: uploads, items and statuses at igni server (service-identity §15)."""

from __future__ import annotations

import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from igni_service.errors import EventRefused, IgniUnavailable
from igni_service.events import (
    MAX_KEY,
    EventAccepted,
    EventSender,
    Producer,
    ServiceEventRequest,
)
from igni_service.service_tokens import ServiceTokens
from igni_service.urls import IgniPaths

#: igni's limits, mirrored so a mistake is caught where it is made.
MAX_TEXT = 65536
MAX_THREAD = 200
MAX_ATTACHMENTS = 10
MAX_TITLE = 200
#: What a person may attach in igni's composer.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

ItemKind = Literal["event", "message", "chat", "action"]
ItemState = Literal["pending", "delivered", "not_handled", "expired", "dropped", "failed"]


class Attachment(BaseModel):
    """A file to send with an item: any type igni's composer takes, at most 5 MiB."""

    model_config = ConfigDict(frozen=True)

    filename: str = Field(min_length=1, max_length=200)
    content_type: str = "application/octet-stream"
    data: bytes = Field(min_length=1, max_length=MAX_ATTACHMENT_BYTES)

    @classmethod
    def from_bytes(cls, filename: str, data: bytes, content_type: str | None = None) -> Attachment:
        guessed = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return cls(filename=filename, content_type=guessed, data=data)

    @classmethod
    def from_path(cls, path: str | Path, content_type: str | None = None) -> Attachment:
        source = Path(path)
        return cls.from_bytes(source.name, source.read_bytes(), content_type)


class AttachmentStored(BaseModel):
    """igni kept the file; an item names it by `attachment_id`."""

    attachment_id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    expires_at: datetime


class ChatAction(BaseModel):
    """What an `action` item does to its thread's chat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["show", "rename", "cancel"]
    view: dict[str, Any] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)


class ServiceItemRequest(BaseModel):
    """`POST /v1/service-items`, as it goes on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    producer: Producer
    kind: ItemKind
    thread: str | None = Field(default=None, min_length=1, max_length=MAX_THREAD)
    text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    start_run: bool = True
    action: ChatAction | None = None
    attachments: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    event_key: str | None = Field(default=None, min_length=1, max_length=MAX_KEY)
    expires_at: datetime | None = None


class ItemStatus(BaseModel):
    """What became of an item: `GET /v1/service-events/{event_id}`."""

    model_config = ConfigDict(extra="ignore")

    event_id: str
    producer: Producer
    kind: ItemKind = "event"
    type: str
    thread: str | None = None
    event_key: str | None = None
    state: ItemState
    dropped_reason: str | None = None
    failed_reason: str | None = None
    #: An event no handler took: why.
    not_handled_reason: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    delivered_at: datetime | None = None
    updated_at: datetime


class AgentWire:
    """The calls behind an `Agent`: uploads, items, statuses. One per service."""

    def __init__(self, tokens: ServiceTokens, sender: EventSender) -> None:
        self._tokens = tokens
        self._sender = sender

    async def upload(self, org_id: str, attachment: Attachment) -> AttachmentStored:
        response = await self._tokens.request(
            "POST",
            IgniPaths.SERVICE_ATTACHMENTS,
            org_id=org_id,
            params={"filename": attachment.filename},
            content=attachment.data,
            headers={"Content-Type": attachment.content_type},
        )
        if response.status_code in (200, 201):
            try:
                return AttachmentStored.model_validate(response.json())
            except (ValidationError, ValueError) as exc:
                raise IgniUnavailable("igni answered the upload with something else") from exc
        if response.status_code != 429 and 400 <= response.status_code < 500:
            raise EventRefused(response.status_code, response.text)
        raise IgniUnavailable(f"igni answered the upload {response.status_code}")

    async def uploaded(self, org_id: str, attachments: list[Attachment]) -> list[str]:
        return [(await self.upload(org_id, a)).attachment_id for a in attachments]

    async def item(self, org_id: str, item: ServiceItemRequest) -> EventAccepted:
        return await self._sender.post(org_id, IgniPaths.SERVICE_ITEMS, item, what=item.kind)

    async def event(self, org_id: str, event: ServiceEventRequest) -> EventAccepted:
        return await self._sender.send(org_id, event)

    async def status(self, org_id: str, event_id: str) -> ItemStatus:
        response = await self._tokens.request(
            "GET", IgniPaths.service_event(event_id), org_id=org_id
        )
        if response.status_code == 200:
            try:
                return ItemStatus.model_validate(response.json())
            except (ValidationError, ValueError) as exc:
                raise IgniUnavailable("igni answered the status with something else") from exc
        if 400 <= response.status_code < 500:
            raise EventRefused(response.status_code, response.text)
        raise IgniUnavailable(f"igni answered the status {response.status_code}")


__all__ = [
    "AgentWire",
    "Attachment",
    "AttachmentStored",
    "ChatAction",
    "ItemKind",
    "ItemState",
    "ItemStatus",
    "ServiceItemRequest",
]
