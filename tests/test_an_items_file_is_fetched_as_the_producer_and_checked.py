"""An item's file is fetched from igni server as the producer, and checked before it is kept.

``docs/CLOUD.md`` §6.1. A service uploads an item's files to igni server;
igni, delivering the item, fetches each with the person's own session and
checks it against the SHA-256 the server stated. Every way that can go
wrong is an :class:`AttachmentUnavailable` — the item is handed out again
rather than delivered with a missing or altered file. igni server is
played by an ``httpx.MockTransport``, the seam
:class:`ServerAttachments` takes for it.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from ember_code.extensions.service_attachments import AttachmentUnavailable, ServerAttachments
from ember_code.extensions.service_items import ItemAttachment
from tests.notification_helpers import API_URL, SESSION, signed_in

CONTENT = b"quarterly figures\n"
ATTACHMENT = ItemAttachment(
    attachment_id="a1",
    filename="figures.csv",
    sha256=hashlib.sha256(CONTENT).hexdigest(),
)


def _server(status: int = 200, content: bytes = CONTENT) -> tuple[httpx.MockTransport, list]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=content)

    return httpx.MockTransport(answer), seen


async def test_the_file_is_fetched_with_the_persons_session_and_returned():
    transport, seen = _server()

    content = await ServerAttachments(signed_in(), transport=transport).fetch("ev1", ATTACHMENT)

    assert content == CONTENT
    (request,) = seen
    assert str(request.url) == f"{API_URL}/v1/me/events/ev1/attachments/a1"
    assert request.headers["Authorization"] == f"Bearer {SESSION}"


async def test_a_file_without_a_stated_hash_is_taken_as_it_comes():
    transport, _ = _server(content=b"anything")
    unhashed = ATTACHMENT.model_copy(update={"sha256": ""})

    assert await ServerAttachments(signed_in(), transport=transport).fetch("ev1", unhashed) == (
        b"anything"
    )


async def test_nobody_signed_in_means_nobody_to_fetch_it_as():
    transport, seen = _server()

    with pytest.raises(AttachmentUnavailable, match="nobody to fetch figures.csv as"):
        await ServerAttachments(signed_in(session=None), transport=transport).fetch(
            "ev1", ATTACHMENT
        )
    assert seen == []


async def test_a_refusal_from_the_server_says_its_status():
    transport, _ = _server(status=404)

    with pytest.raises(AttachmentUnavailable, match="igni server answered 404"):
        await ServerAttachments(signed_in(), transport=transport).fetch("ev1", ATTACHMENT)


async def test_a_server_that_cannot_be_reached_is_unavailable():
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(AttachmentUnavailable, match="could not be reached"):
        await ServerAttachments(signed_in(), transport=httpx.MockTransport(unreachable)).fetch(
            "ev1", ATTACHMENT
        )


async def test_a_file_that_does_not_match_its_hash_is_refused():
    transport, _ = _server(content=b"quarterly figures, altered\n")

    with pytest.raises(AttachmentUnavailable, match="does not match its SHA-256"):
        await ServerAttachments(signed_in(), transport=transport).fetch("ev1", ATTACHMENT)


async def test_a_file_larger_than_igni_accepts_is_refused(monkeypatch):
    monkeypatch.setattr(ServerAttachments, "MAX_BYTES", len(CONTENT) - 1)
    transport, _ = _server()

    with pytest.raises(AttachmentUnavailable, match="larger than igni accepts"):
        await ServerAttachments(signed_in(), transport=transport).fetch("ev1", ATTACHMENT)
