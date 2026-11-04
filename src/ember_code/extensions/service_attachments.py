"""The files an item names, fetched from igni server with the producer's own credential.

``docs/CLOUD.md`` §6.1. A service uploads an item's files to igni server
first; igni, delivering the item, fetches each —
``GET /v1/me/events/{event_id}/attachments/{attachment_id}`` with the
person's session on the desktop, with the VM's own token in the cloud —
checks it against the SHA-256 igni server stated, and hands the bytes to
the chat the item lands in, which keeps them as it keeps a person's upload.
"""

from __future__ import annotations

import hashlib
from typing import ClassVar, Protocol

import httpx

from ember_code.core.activity.http import busy_call
from ember_code.extensions.service_items import ItemAttachment
from ember_code.extensions.service_token import NotSignedIn
from ember_code.notifications.server_client import SignedInServer


class AttachmentUnavailable(Exception):
    """An item's file could not be fetched, or did not match: the item is handed out again."""


class AttachmentSource(Protocol):
    async def fetch(self, event_id: str, attachment: ItemAttachment) -> bytes: ...


class ServerAttachments:
    """Fetches an item's files from igni server, as the producer."""

    PATH: ClassVar[str] = "/v1/me/events/{event_id}/attachments/{attachment_id}"
    #: igni server keeps none larger (the composer's limit), with room to spare.
    MAX_BYTES: ClassVar[int] = 6 * 1024 * 1024
    TIMEOUT_SECONDS: ClassVar[float] = 30.0

    def __init__(
        self, server: SignedInServer, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._server = server
        self._transport = transport

    async def fetch(self, event_id: str, attachment: ItemAttachment) -> bytes:
        try:
            api_url, session = self._server.current()
        except NotSignedIn as exc:
            raise AttachmentUnavailable(f"nobody to fetch {attachment.filename} as: {exc}") from exc
        path = self.PATH.format(event_id=event_id, attachment_id=attachment.attachment_id)
        try:
            async with (
                busy_call("igni-server", "an item's attachment"),
                httpx.AsyncClient(
                    timeout=self.TIMEOUT_SECONDS, transport=self._transport
                ) as client,
            ):
                response = await client.get(
                    f"{api_url}{path}",
                    headers={"Authorization": f"Bearer {session.access_token}"},
                )
        except httpx.HTTPError as exc:
            raise AttachmentUnavailable(f"igni server could not be reached: {exc}") from exc
        if response.status_code != 200:
            raise AttachmentUnavailable(
                f"{attachment.filename}: igni server answered {response.status_code}"
            )
        content = response.content
        if len(content) > self.MAX_BYTES:
            raise AttachmentUnavailable(f"{attachment.filename} is larger than igni accepts")
        if attachment.sha256 and hashlib.sha256(content).hexdigest() != attachment.sha256:
            raise AttachmentUnavailable(f"{attachment.filename} does not match its SHA-256")
        return content


__all__ = ["AttachmentSource", "AttachmentUnavailable", "ServerAttachments"]
