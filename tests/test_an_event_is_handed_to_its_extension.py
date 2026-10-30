"""A service's event always starts or resumes the producer's agent run, once.

``extensions/service_events.py`` (igni server's service-identity §15). The
platform hands events at least once; igni delivers each once — across
restarts — asks the extension it is for what the agent should see, and
starts a run with it, or, while a run is going (working or waiting on an
approval), queues it for that run.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ember_code.backend.activity import ActivityState
from ember_code.backend.service_event_delivery import ExtensionEventHandler, MainSession
from ember_code.core.db.database import Database
from ember_code.core.db.urls import DatabaseUrl
from ember_code.extensions.host.hooks import ExtensionHooks, HookFailure
from ember_code.extensions.host.wire import HookAnswer, Outcome
from ember_code.extensions.service_events import (
    DeliveredEvents,
    EventPrompt,
    ServiceEvent,
    ServiceEventInbox,
)
from ember_code.protocol import messages as msg
from tests.host_model_helpers import HostRig
from tests.storage_backends import Scopes


def _event(event_id: str = "evt-1", **extra: Any) -> ServiceEvent:
    fields: dict[str, Any] = {
        "event_id": event_id,
        "extension_id": "acme.indexer",
        "type": "index.ready",
        "payload": {"repo": "acme/api"},
    }
    return ServiceEvent.model_validate({**fields, **extra})


class Session:
    """The main session, as the inbox sees it."""

    def __init__(self, *, busy: bool = False) -> None:
        self.is_busy = busy
        self.posted: list[tuple[str, str, bool]] = []
        self.refuse = False

    def busy(self) -> bool:
        return self.is_busy

    async def post(self, *, extension_id: str, text: str, queued: bool) -> None:
        if self.refuse:
            raise RuntimeError("the session is gone")
        self.posted.append((extension_id, text, queued))


@pytest.fixture
def delivered(database_url: DatabaseUrl) -> DeliveredEvents:
    return DeliveredEvents(Database(database_url, scope=Scopes.fresh()))


async def test_with_nothing_going_on_an_event_starts_a_run(delivered: DeliveredEvents):
    session = Session()
    receipt = await ServiceEventInbox(delivered=delivered, session=session).deliver([_event()])
    assert receipt.delivered == ["evt-1"] and receipt.acknowledge == ["evt-1"]
    [(extension, text, queued)] = session.posted
    assert extension == "acme.indexer" and not queued, "starts a run"
    assert "index.ready" in text and "acme/api" in text


async def test_while_a_run_works_or_waits_on_an_approval_it_is_queued_for_it(
    delivered: DeliveredEvents,
):
    session = Session(busy=True)
    await ServiceEventInbox(delivered=delivered, session=session).deliver([_event()])
    assert [queued for _, _, queued in session.posted] == [True]


async def test_an_event_handed_out_again_is_acknowledged_and_dropped_even_after_a_restart(
    delivered: DeliveredEvents,
):
    session = Session()
    first = await ServiceEventInbox(delivered=delivered, session=session).deliver([_event()])
    assert first.delivered == ["evt-1"]
    restarted = ServiceEventInbox(delivered=delivered, session=session)
    again = await restarted.deliver([_event(), _event("evt-2")])
    assert again.duplicate == ["evt-1"] and again.delivered == ["evt-2"]
    assert again.acknowledge == ["evt-2", "evt-1"]
    assert len(session.posted) == 2, "one run per event, not per delivery"


async def test_one_the_session_cannot_take_is_left_to_be_handed_out_again(
    delivered: DeliveredEvents,
):
    session = Session()
    session.refuse = True
    inbox = ServiceEventInbox(delivered=delivered, session=session)
    receipt = await inbox.deliver([_event()])
    assert receipt.failed == ["evt-1"] and receipt.acknowledge == []
    session.refuse = False
    assert (await inbox.deliver([_event()])).delivered == ["evt-1"], "not recorded before"


async def test_the_extensions_text_is_what_the_agent_sees(delivered: DeliveredEvents):
    session = Session()

    async def ask(event: ServiceEvent) -> str | None:
        return f"Re-run the review: {event.payload['repo']} is indexed."

    await ServiceEventInbox(delivered=delivered, session=session, ask_extension=ask).deliver(
        [_event()]
    )
    assert session.posted[0][1] == "Re-run the review: acme/api is indexed."


async def test_an_extension_that_fails_or_is_silent_leaves_igni_to_write_it(
    delivered: DeliveredEvents,
):
    session = Session()

    async def broken(event: ServiceEvent) -> str | None:
        raise HookFailure("its process is not connected", unanswered=True)

    await ServiceEventInbox(delivered=delivered, session=session, ask_extension=broken).deliver(
        [_event()]
    )
    assert session.posted[0][1] == EventPrompt.of(_event())


def test_a_huge_payload_is_cut_for_the_agent():
    text = EventPrompt.of(_event(payload={"log": "x" * 50_000}))
    assert len(text) < EventPrompt.MAX_PAYLOAD_CHARS + 500 and "(cut)" in text


# ── The extension is asked over its socket ────────────────────────


async def test_the_extension_is_asked_and_its_prompt_taken():
    hooks = ExtensionHooks()
    frames: list[dict[str, Any]] = []

    def send(frame: str) -> None:
        sent = json.loads(frame)
        frames.append(sent)
        hooks.answered(
            "acme.indexer",
            HookAnswer(hook=sent["id"], outcome=Outcome.MODIFY, value={"prompt": "Do it."}),
        )

    hooks.connect("acme.indexer", send)
    assert await ExtensionEventHandler(hooks)(_event()) == "Do it."
    [frame] = frames
    assert frame["event"] == "ServiceEvent" and frame["value"]["event_id"] == "evt-1"


async def test_an_extension_that_allows_adds_nothing():
    hooks = ExtensionHooks()

    def send(frame: str) -> None:
        hooks.answered(
            "acme.indexer", HookAnswer(hook=json.loads(frame)["id"], outcome=Outcome.ALLOW)
        )

    hooks.connect("acme.indexer", send)
    assert await ExtensionEventHandler(hooks)(_event()) is None


# ── The main session ──────────────────────────────────────────────


async def test_the_main_session_is_busy_unless_idle():
    states = [ActivityState.IDLE]
    posted: list[dict[str, Any]] = []

    class Runtime:
        backend = type(
            "Backend", (), {"activity": lambda self: type("A", (), {"state": states[0]})()}
        )()

        def current_session_id(self) -> str:
            return "s-main"

    class Orchestrator:
        pool = type("Pool", (), {"default": Runtime()})()

        async def post_from_extension(self, **kwargs: Any) -> None:
            posted.append(kwargs)

    session = MainSession(Orchestrator())
    assert not session.busy()
    states[0] = ActivityState.WAITING
    assert session.busy(), "waiting on an approval: the event waits for that run"
    await session.post(extension_id="acme.indexer", text="hi", queued=True)
    assert posted == [
        {
            "session_id": "s-main",
            "extension_id": "acme.indexer",
            "text": "hi",
            "media": None,
            "queued": True,
        }
    ]


# ── On the wire ───────────────────────────────────────────────────


async def test_the_platform_delivers_over_the_rpc_and_is_told_what_to_acknowledge(tmp_path):
    rig = await HostRig(tmp_path).start()
    try:
        session = Session()
        rig.orchestrator.attach_event_inbox(  # type: ignore[union-attr]
            ServiceEventInbox(
                delivered=DeliveredEvents(Database(DatabaseUrl.for_file(tmp_path / "e.db"))),
                session=session,
            )
        )
        request = msg.RPCRequest(
            id="d1",
            method="deliver_service_events",
            args={"events": [_event().model_dump(mode="json")]},
        )
        await rig.orchestrator.dispatch(request)  # type: ignore[union-attr]
        [answer] = [m for m in rig.windows.of(msg.RPCResponse) if m.id == "d1"]
        assert answer.error is None and answer.result["acknowledge"] == ["evt-1"]
        await asyncio.sleep(0)
        assert len(session.posted) == 1
    finally:
        await rig.stop()
