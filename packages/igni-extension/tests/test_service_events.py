"""`@ext.on_service_event`: an event the extension's service sent is the extension's to act on.

igni posts nothing for an event; the handler acts through its host calls.
The answer only says whether a handler took it — `allow` — or not:
`reject` with no handler for its type, `failed` when the handler raised.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from igni_extension import IgniExtension, ServiceEvent
from igni_extension.testing import FakeHost

EVENT = {
    "event_id": "evt-1",
    "extension_id": "acme.example",
    "type": "index.ready",
    "payload": {"repo": "acme/api"},
}


@pytest.fixture
async def host() -> AsyncIterator[FakeHost]:
    async with FakeHost() as fake:
        yield fake


async def _connected(ext: IgniExtension) -> asyncio.Task[None]:
    await ext.connect()
    return asyncio.create_task(ext.connection.serve_forever())


async def _answer(ext: IgniExtension, host: FakeHost) -> dict[str, Any]:
    task = await _connected(ext)
    try:
        return await host.hook("ServiceEvent", EVENT)
    finally:
        task.cancel()
        await ext.aclose()


async def test_the_handler_for_the_type_takes_it_and_igni_posts_nothing(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    seen: list[ServiceEvent] = []

    @ext.on_service_event("index.ready")
    async def ready(event: ServiceEvent) -> None:
        seen.append(event)

    answer = await _answer(ext, host)
    assert answer["outcome"] == "allow" and "value" not in answer
    assert [e.payload for e in seen] == [{"repo": "acme/api"}]


async def test_without_a_handler_for_its_type_it_is_not_handled(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.on_service_event("build.failed")
    def failed(event: ServiceEvent) -> None:
        return None

    answer = await _answer(ext, host)
    assert answer["outcome"] == "reject"
    assert answer["reason"] == "no handler for index.ready"


async def test_a_handler_that_raises_is_reported_failed(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.on_service_event("index.ready")
    def broken(event: ServiceEvent) -> None:
        raise RuntimeError("the index service is down")

    answer = await _answer(ext, host)
    assert answer["outcome"] == "failed" and "the index service is down" in answer["reason"]


async def test_a_catch_all_handles_any_other_type(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    seen: list[str] = []

    @ext.on_service_event()
    def anything(event: ServiceEvent) -> None:
        seen.append(event.type)

    answer = await _answer(ext, host)
    assert seen == ["index.ready"] and answer["outcome"] == "allow"


async def test_returned_text_is_no_longer_posted_and_says_so(
    host: FakeHost, caplog: pytest.LogCaptureFixture
) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.on_service_event("index.ready")
    def old_style(event: ServiceEvent) -> str:
        return "Re-run the review."

    with caplog.at_level(logging.WARNING):
        answer = await _answer(ext, host)
    assert answer["outcome"] == "allow" and "value" not in answer
    assert "no longer posts" in caplog.text


def test_a_type_has_one_handler() -> None:
    ext = IgniExtension("acme.example", environ={})
    ext.on_service_event("x")(lambda event: None)
    with pytest.raises(ValueError, match="has a handler already"):
        ext.on_service_event("x")(lambda event: None)
