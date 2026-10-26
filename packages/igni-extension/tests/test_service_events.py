"""`@ext.on_service_event`: what the agent sees of an event the extension's service sent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

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


async def test_the_handler_for_the_type_writes_the_agents_text(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.on_service_event("index.ready")
    async def ready(event: ServiceEvent) -> str:
        return f"The index for {event.payload['repo']} is ready."

    task = await _connected(ext)
    try:
        answer = await host.hook("ServiceEvent", EVENT)
    finally:
        task.cancel()
        await ext.aclose()
    assert answer["outcome"] == "modify"
    assert answer["value"] == {"prompt": "The index for acme/api is ready."}


async def test_without_a_handler_igni_writes_it(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.on_service_event("build.failed")
    def failed(event: ServiceEvent) -> None:
        return None

    task = await _connected(ext)
    try:
        answer = await host.hook("ServiceEvent", EVENT)
    finally:
        task.cancel()
        await ext.aclose()
    assert answer["outcome"] == "allow"


async def test_a_catch_all_handles_any_other_type(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    seen: list[str] = []

    @ext.on_service_event()
    def anything(event: ServiceEvent) -> str:
        seen.append(event.type)
        return "Something happened."

    task = await _connected(ext)
    try:
        answer = await host.hook("ServiceEvent", EVENT)
    finally:
        task.cancel()
        await ext.aclose()
    assert seen == ["index.ready"] and answer["value"]["prompt"] == "Something happened."


def test_a_type_has_one_handler() -> None:
    ext = IgniExtension("acme.example", environ={})
    ext.on_service_event("x")(lambda event: None)
    with pytest.raises(ValueError, match="has a handler already"):
        ext.on_service_event("x")(lambda event: None)
