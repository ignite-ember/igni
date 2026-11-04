"""A message a service sent is marked as the service's in ``chat.message_added``.

``docs/CLOUD.md`` §6.1: a service's message lands as sent, posted as its
extension's. The extension only watches it — as every post is watched,
``chat.message_added`` — and the signal says which service sent it
(``from_service``, the service's client id), beside the extension it is
labelled as (``from_extension``). A post the extension made itself, and
the person's own message, carry no ``from_service``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember_code.backend.service_item_desk import PoolChatDesk
from ember_code.protocol import messages as msg
from tests.host_model_helpers import HostRig, Sink

SERVICE = "svc-claims"


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def watching(rig: HostRig, extension_id: str) -> Sink:
    sink = Sink()
    assert rig.model is not None
    rig.model.bus.subscribe(rig.model.bus.attach(extension_id, sink), ["chat.message_added"])
    return sink


def added(sink: Sink, text: str) -> dict[str, Any]:
    [value] = [v for v in sink.values("chat.message_added") if v["text"] == text]
    return value


async def test_a_message_its_service_sent_carries_the_service_and_the_extension(
    rig: HostRig,
) -> None:
    ext = rig.install("acme.claims", host=["chat.read"])
    sink = watching(rig, ext)
    assert rig.orchestrator is not None
    desk = PoolChatDesk(rig.orchestrator)
    chat = await desk.open_chat("Claim CL-20431")

    posted = await desk.deliver(
        session_id=chat, extension_id=ext, from_service=SERVICE, text="Claim arrived."
    )
    await desk.queue(session_id=chat, extension_id=ext, from_service=SERVICE, text="Photos next.")
    await rig.settle()

    assert posted.ok and not posted.queued
    started = added(sink, "Claim arrived.")
    assert started["from_service"] == SERVICE
    assert started["from_extension"] == ext, "posted as the extension's all the same"
    assert started["session_id"] == chat and not started["queued"]
    queued = added(sink, "Photos next.")
    assert queued["from_service"] == SERVICE and queued["queued"]
    echoes = [m for m in rig.windows.of(msg.UserMessageReceived) if m.session_id == chat]
    assert {e.service_client_id for e in echoes} == {SERVICE}


async def test_a_post_the_extension_made_itself_has_no_service(rig: HostRig) -> None:
    ext = rig.install("acme.claims", host=["chat.read", "chat.post"])
    sink = watching(rig, ext)

    reply = await rig.ask(ext, "call", "chat.post", text="From the extension.")
    assert reply.ok, reply.error
    await rig.settle()

    value = added(sink, "From the extension.")
    assert value["from_extension"] == ext
    assert value["from_service"] is None


async def test_the_persons_message_has_neither(rig: HostRig) -> None:
    ext = rig.install("acme.claims", host=["chat.read"])
    sink = watching(rig, ext)
    assert rig.orchestrator is not None

    await rig.orchestrator.dispatch(msg.UserMessage(text="From the person.", client_id="ws-1"))
    await rig.settle()

    value = added(sink, "From the person.")
    assert value["from_extension"] is None
    assert value["from_service"] is None
