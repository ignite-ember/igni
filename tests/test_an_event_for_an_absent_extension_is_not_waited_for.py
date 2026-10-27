"""An event for an extension that is not here goes to the run at once.

The integration run measured 30.8 s to deliver one event to a VM whose
extension was not running: igni waited its full 30 s for the process to
connect — exactly the gateway's deadline for the delivery — so the
event was never acknowledged and was handed out again, in a loop. Now an
extension not installed (or not running) here is not waited for, one
installed but not connected yet only briefly, an answer for at most
:attr:`ExtensionEventHandler.TIMEOUT_S`, and a batch's extensions are
asked at once — all well inside the gateway's deadline.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from ember_code.backend.service_event_delivery import ExtensionEventHandler
from ember_code.core.db.database import Database
from ember_code.core.db.urls import DatabaseUrl
from ember_code.extensions.host.hooks import ExtensionHooks, HookFailure
from ember_code.extensions.host.wire import HookAnswer, Outcome
from ember_code.extensions.registry import ExtensionRegistry
from ember_code.extensions.service_events import (
    DeliveredEvents,
    EventPrompt,
    ServiceEvent,
    ServiceEventInbox,
)
from tests.host_model_helpers import manifest
from tests.storage_backends import Scopes


def _event(event_id: str = "evt-1", extension_id: str = "acme.indexer") -> ServiceEvent:
    return ServiceEvent.model_validate(
        {
            "event_id": event_id,
            "extension_id": extension_id,
            "type": "index.ready",
            "payload": {"repo": "acme/api"},
        }
    )


class Session:
    def __init__(self) -> None:
        self.posted: list[str] = []

    def busy(self) -> bool:
        return False

    async def post(self, *, extension_id: str, text: str, queued: bool) -> None:
        self.posted.append(text)


@pytest.fixture
def delivered(database_url: DatabaseUrl) -> DeliveredEvents:
    return DeliveredEvents(Database(database_url, scope=Scopes.fresh()))


def _hooks(*installed: str) -> ExtensionHooks:
    registry = ExtensionRegistry()
    for extension_id in installed:
        registry.install(manifest(extension_id))
    hooks = ExtensionHooks()
    hooks.attach(registry)
    return hooks


async def test_an_extension_not_installed_here_is_not_waited_for() -> None:
    started = time.perf_counter()
    with pytest.raises(HookFailure, match="not installed"):
        await _hooks().ask_one("acme.indexer", "ServiceEvent", {}, timeout=30)
    assert time.perf_counter() - started < 0.1


async def test_one_installed_but_not_connected_is_waited_for_briefly() -> None:
    started = time.perf_counter()
    with pytest.raises(HookFailure, match="not connected"):
        await _hooks("acme.indexer").ask_one(
            "acme.indexer", "ServiceEvent", {}, timeout=30, connect_timeout=0.1
        )
    assert time.perf_counter() - started < 1


async def test_the_event_reaches_the_run_at_once_as_it_came(delivered: DeliveredEvents) -> None:
    session = Session()
    inbox = ServiceEventInbox(
        delivered=delivered, session=session, ask_extension=ExtensionEventHandler(_hooks())
    )
    started = time.perf_counter()
    receipt = await inbox.deliver([_event()])
    assert time.perf_counter() - started < 1
    assert receipt.acknowledge == ["evt-1"]
    assert session.posted == [EventPrompt.of(_event())]


def test_every_wait_is_well_inside_the_gateways_deadline() -> None:
    gateway_deadline = 30.0
    assert ExtensionEventHandler.CONNECT_TIMEOUT_S <= ExtensionEventHandler.TIMEOUT_S
    assert gateway_deadline / 2 >= ExtensionEventHandler.TIMEOUT_S


async def test_a_batch_asks_its_extensions_at_once(delivered: DeliveredEvents) -> None:
    async def slow(event: ServiceEvent) -> str:
        await asyncio.sleep(0.3)
        return f"about {event.event_id}"

    session = Session()
    inbox = ServiceEventInbox(delivered=delivered, session=session, ask_extension=slow)
    started = time.perf_counter()
    receipt = await inbox.deliver([_event("e1"), _event("e2"), _event("e3"), _event("e1")])
    assert time.perf_counter() - started < 0.8, "the slowest answer, not their sum"
    assert receipt.delivered == ["e1", "e2", "e3"] and receipt.duplicate == ["e1"]
    assert session.posted == ["about e1", "about e2", "about e3"], "posted in order"


async def test_a_connected_extension_still_answers(delivered: DeliveredEvents) -> None:
    hooks = _hooks("acme.indexer")

    def send(frame: str) -> None:
        sent: dict[str, Any] = json.loads(frame)
        hooks.answered(
            "acme.indexer",
            HookAnswer(hook=sent["id"], outcome=Outcome.MODIFY, value={"prompt": "Reindex."}),
        )

    hooks.connect("acme.indexer", send)
    assert await ExtensionEventHandler(hooks)(_event()) == "Reindex."
