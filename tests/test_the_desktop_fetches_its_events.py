"""On the desktop igni pulls the person's service events itself, while online.

igni server's service-identity §15: ``POST /v1/me/events/pull`` with the
person's session, each event through the inbox, ``POST /v1/me/events/ack``
with what to acknowledge.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ember_code.core.layer import Layer
from ember_code.extensions.event_pull import PersonEventPull, PullFailed, PullRefused
from ember_code.extensions.service_events import DeliveryReceipt, NotHandled, ServiceEvent
from ember_code.extensions.service_token import IgniSession, NotSignedIn
from ember_code.notifications.server_client import SignedInServer


def _event(event_id: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "extension_id": "acme.indexer",
        "client_id": "c1",
        "type": "index.ready",
        "payload": {"repo": "acme/api"},
        "event_key": None,
        "created_at": "2026-10-27T09:00:00Z",
        "expires_at": "2026-11-10T09:00:00Z",
    }


class Igni:
    def __init__(self, events: list[dict[str, Any]], status: int = 200) -> None:
        self.events = events
        self.status = status
        self.acked: list[list[str]] = []
        self.not_handled: list[list[dict[str, str]]] = []
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer the-session"
        if self.status != 200:
            return httpx.Response(self.status)
        if request.url.path == PersonEventPull.PULL_PATH:
            return httpx.Response(200, json={"events": self.events})
        body = json.loads(request.content)
        ids = body["event_ids"]
        self.acked.append(ids)
        self.not_handled.append(body["not_handled"])
        return httpx.Response(200, json={"delivered": len(ids)})


class Inbox:
    def __init__(self) -> None:
        self.got: list[str] = []

    async def deliver(self, events: list[ServiceEvent]) -> DeliveryReceipt:
        self.got += [e.event_id for e in events]
        return DeliveryReceipt(
            delivered=[e.event_id for e in events if e.event_id not in ("bad", "unseen")],
            failed=[e.event_id for e in events if e.event_id == "bad"],
            not_handled=[
                NotHandled(event_id=e.event_id, reason="no handler for index.ready")
                for e in events
                if e.event_id == "unseen"
            ],
        )


def _pull(igni: Igni, *, signed_in: bool = True) -> tuple[PersonEventPull, Inbox]:
    inbox = Inbox()
    server = SignedInServer(
        api_url=lambda: "https://igni.test",
        session=lambda: IgniSession(access_token="the-session") if signed_in else None,
    )
    pull = PersonEventPull(
        server=server,
        inbox=inbox,  # type: ignore[arg-type]
        transport=httpx.MockTransport(igni.handle),
    )
    return pull, inbox


async def test_pulled_events_are_delivered_and_what_was_taken_acknowledged():
    igni = Igni([_event("e1"), _event("bad"), _event("e2")])
    pull, inbox = _pull(igni)
    pulled, receipt = await pull.once()
    assert pulled == 3 and inbox.got == ["e1", "bad", "e2"]
    assert igni.acked == [["e1", "e2"]], "the one not taken comes back after its lease"
    assert receipt.failed == ["bad"]


async def test_an_event_no_handler_took_is_acknowledged_and_reported_not_handled():
    igni = Igni([_event("e1"), _event("unseen")])
    pull, _ = _pull(igni)
    await pull.once()
    assert igni.acked == [["e1", "unseen"]]
    assert igni.not_handled == [[{"event_id": "unseen", "reason": "no handler for index.ready"}]]


async def test_nothing_pulled_acknowledges_nothing():
    igni = Igni([])
    pull, _ = _pull(igni)
    assert (await pull.once())[0] == 0
    assert igni.calls == [PersonEventPull.PULL_PATH]


async def test_nobody_signed_in_and_an_apps_token_and_an_unreachable_server():
    with pytest.raises(NotSignedIn):
        await _pull(Igni([]), signed_in=False)[0].once()
    with pytest.raises(PullRefused):
        await _pull(Igni([], status=403))[0].once()
    with pytest.raises(PullFailed):
        await _pull(Igni([], status=503))[0].once()


def test_the_desktop_fetches_and_a_layer_may_not():
    class Platformed(Layer):
        def fetches_own_events(self) -> bool:
            return False

    assert Layer().fetches_own_events()
    assert not Platformed().fetches_own_events()
