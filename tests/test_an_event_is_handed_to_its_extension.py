"""A service's event is its extension's: igni hands it over and posts nothing.

``extensions/service_events.py`` (igni server's service-identity §15,
``docs/CLOUD.md`` §6.1). The platform hands events at least once; igni
hands each to the extension it is for, once — across restarts — and
posts nothing for it: the agent runs only if the handler posts, through
its own host calls. An event no handler took — none for its type, the
extension not running, the handler failing — is *not handled*, and igni
server is told so, so the service reads it. Never a silent default post.
"""

from __future__ import annotations

import json
from typing import Any

from ember_code.backend.activity import ActivityState
from ember_code.backend.service_event_delivery import ExtensionEventHandler
from ember_code.backend.service_item_desk import PoolChatDesk
from ember_code.core.db.urls import DatabaseUrl
from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.wire import HookAnswer, Outcome
from ember_code.extensions.service_events import EventHandling, NotHandled, ServiceEvent
from ember_code.protocol import messages as msg
from tests.host_model_helpers import HostRig
from tests.service_item_rig import Inbox


def _event(event_id: str = "evt-1", **extra: Any) -> ServiceEvent:
    fields: dict[str, Any] = {
        "event_id": event_id,
        "extension_id": "acme.indexer",
        "client_id": "svc-indexer",
        "type": "index.ready",
        "payload": {"repo": "acme/api"},
    }
    return ServiceEvent.model_validate({**fields, **extra})


class Extension:
    """The extension's handlers, as igni hands it events."""

    def __init__(self, *, handles: bool = True) -> None:
        self.handles = handles
        self.handed: list[str] = []

    async def __call__(self, event: ServiceEvent) -> EventHandling:
        self.handed.append(event.event_id)
        if not self.handles:
            return EventHandling(handled=False, reason=f"no handler for {event.type}")
        return EventHandling(handled=True)


async def test_an_event_is_handed_to_its_extension_and_igni_posts_nothing(
    database_url: DatabaseUrl,
):
    extension = Extension()
    rig = Inbox(database_url, hand=extension)
    receipt = await rig.deliver(_event())

    assert extension.handed == ["evt-1"]
    assert receipt.delivered == ["evt-1"] and receipt.acknowledge == ["evt-1"]
    assert rig.desk.posts == [] and len(rig.desk.titles) == 1, "no post, no chat opened"


async def test_an_event_no_handler_took_is_reported_not_handled_never_posted(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url, hand=Extension(handles=False))
    receipt = await rig.deliver(_event())

    assert receipt.not_handled == [
        NotHandled(event_id="evt-1", reason="no handler for index.ready")
    ]
    assert receipt.acknowledge == ["evt-1"], "handing it out again would not change that"
    assert receipt.delivered == [] and rig.desk.posts == []


async def test_without_any_extension_wiring_every_event_is_not_handled(database_url: DatabaseUrl):
    receipt = await Inbox(database_url).deliver(_event())
    assert [n.event_id for n in receipt.not_handled] == ["evt-1"]


async def test_a_handler_that_cannot_be_reached_leaves_the_event_not_handled(
    database_url: DatabaseUrl,
):
    async def gone(event: ServiceEvent) -> EventHandling:
        raise ConnectionError("the host socket closed")

    receipt = await Inbox(database_url, hand=gone).deliver(_event())
    [missed] = receipt.not_handled
    assert "the host socket closed" in missed.reason


async def test_an_event_handed_out_again_is_acknowledged_and_dropped_even_after_a_restart(
    database_url: DatabaseUrl,
):
    extension = Extension()
    rig = Inbox(database_url, hand=extension)
    assert (await rig.deliver(_event())).delivered == ["evt-1"]
    again = await rig.restarted(hand=extension).deliver([_event(), _event("evt-2")])
    assert again.duplicate == ["evt-1"] and again.delivered == ["evt-2"]
    assert extension.handed == ["evt-1", "evt-2"], "handed over once per event"


async def test_an_event_not_handled_is_asked_about_again_if_handed_out_again(
    database_url: DatabaseUrl,
):
    extension = Extension(handles=False)
    rig = Inbox(database_url, hand=extension)
    await rig.deliver(_event())
    extension.handles = True
    assert (await rig.deliver(_event())).delivered == ["evt-1"]


# ── The extension is asked over its socket ────────────────────────


def _answering(hooks: ExtensionHooks, answer: HookAnswer) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    def send(frame: str) -> None:
        sent = json.loads(frame)
        frames.append(sent)
        hooks.answered("acme.indexer", answer.model_copy(update={"hook": sent["id"]}))

    hooks.connect("acme.indexer", send)
    return frames


async def test_a_handler_that_ran_is_handled():
    hooks = ExtensionHooks()
    frames = _answering(hooks, HookAnswer(hook="", outcome=Outcome.ALLOW))
    assert await ExtensionEventHandler(hooks)(_event()) == EventHandling(handled=True)
    [frame] = frames
    assert frame["event"] == "ServiceEvent" and frame["value"]["event_id"] == "evt-1"


async def test_no_handler_or_a_failing_one_is_not_handled():
    hooks = ExtensionHooks()
    _answering(hooks, HookAnswer(hook="", outcome=Outcome.REJECT, reason="no handler for x"))
    assert await ExtensionEventHandler(hooks)(_event()) == EventHandling(
        handled=False, reason="no handler for x"
    )
    failing = ExtensionHooks()
    _answering(failing, HookAnswer(hook="", outcome=Outcome.FAILED, reason="boom"))
    assert not (await ExtensionEventHandler(failing)(_event())).handled


async def test_text_an_older_handler_returns_is_not_posted():
    hooks = ExtensionHooks()
    _answering(hooks, HookAnswer(hook="", outcome=Outcome.MODIFY, value={"prompt": "Do it."}))
    assert await ExtensionEventHandler(hooks)(_event()) == EventHandling(handled=True)


# ── A chat of the pool ────────────────────────────────────────────


async def test_a_chat_of_the_pool_is_busy_unless_idle_and_a_post_waits_for_its_run():
    states = [ActivityState.IDLE]
    posted: list[dict[str, Any]] = []
    decided: list[Any] = []

    class Runtime:
        backend = type(
            "Backend",
            (),
            {
                "activity": lambda self: type("A", (), {"state": states[0]})(),
                "act": lambda self, *a, **k: decided.append(a),
            },
        )()

    class Pool:
        def find(self, session_id: str) -> Runtime:
            return Runtime()

    class Orchestrator:
        pool = Pool()

        async def post_from_extension(self, **kwargs: Any) -> None:
            posted.append(kwargs)

    desk = PoolChatDesk(Orchestrator())
    assert not desk.busy("s-claim")
    states[0] = ActivityState.WAITING
    assert desk.busy("s-claim") and desk.waiting_on_approval("s-claim")
    answer = await desk.deliver(
        session_id="s-claim", extension_id="acme.indexer", from_service="svc", text="hi"
    )
    assert answer.ok and answer.queued, "waiting on an approval: it waits for that run"
    assert posted == [
        {
            "session_id": "s-claim",
            "extension_id": "acme.indexer",
            "text": "hi",
            "media": None,
            "queued": True,
            "from_service": "svc",
        }
    ]
    assert decided == [], "the approval is the person's: nothing answered it"


# ── On the wire ───────────────────────────────────────────────────


async def test_the_platform_delivers_over_the_rpc_and_is_told_what_was_not_handled(tmp_path):
    rig = await HostRig(tmp_path).start()
    try:
        items = Inbox(DatabaseUrl.for_file(tmp_path / "e.db"), hand=Extension(handles=False))
        rig.orchestrator.attach_event_inbox(items.inbox)  # type: ignore[union-attr]
        request = msg.RPCRequest(
            id="d1",
            method="deliver_service_events",
            args={"events": [_event().model_dump(mode="json")]},
        )
        await rig.orchestrator.dispatch(request)  # type: ignore[union-attr]
        [answer] = [m for m in rig.windows.of(msg.RPCResponse) if m.id == "d1"]
        assert answer.error is None and answer.result["acknowledge"] == ["evt-1"]
        assert answer.result["not_handled"] == [
            {"event_id": "evt-1", "reason": "no handler for index.ready"}
        ]
        assert items.desk.posts == []
    finally:
        await rig.stop()
