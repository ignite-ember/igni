"""`host.agent(producer)`: messages, files, chats and events, on the §15 wire.

A message, a chat or an action goes to `POST /v1/service-items` and is
posted by the producer's igni as sent; an event goes to
`POST /v1/service-events` and is its extension's. Files are uploaded first
and named by id. What became of each is read back by polling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_service import Attachment, EventRefused, IgniService, Producer
from igni_service.testing import FakeIgni
from igni_service.urls import IgniPaths

from .conftest import ORG

MARIEKE = Producer.person("u-max")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


async def test_a_message_goes_on_the_wire_as_igni_reads_it(
    igni: IgniService, fake: FakeIgni
) -> None:
    accepted = (
        await igni.host(ORG)
        .agent(MARIEKE)
        .send_message(
            "Claim CL-20431 arrived; assess it.", thread="claim:CL-20431", event_key="claim:1:new"
        )
    )

    [seen] = fake.requests_to(IgniPaths.SERVICE_ITEMS)
    assert seen.json_body() == {
        "producer": {"kind": "person", "id": "u-max"},
        "kind": "message",
        "thread": "claim:CL-20431",
        "text": "Claim CL-20431 arrived; assess it.",
        "start_run": True,
        "event_key": "claim:1:new",
    }
    assert accepted.event_id == fake.events[0].event_id


async def test_files_are_uploaded_first_and_named_by_id(
    igni: IgniService, fake: FakeIgni, tmp_path: Path
) -> None:
    form = tmp_path / "claim form.pdf"
    form.write_bytes(b"%PDF-1.7")

    await (
        igni.host(ORG)
        .agent(MARIEKE)
        .send_message(
            "See the photo and the form.",
            attachments=[Attachment.from_bytes("damage.png", PNG), Attachment.from_path(form)],
        )
    )

    photo, pdf = fake.requests_to(IgniPaths.SERVICE_ATTACHMENTS)
    assert (photo.query["filename"], photo.headers["content-type"], photo.body) == (
        ["damage.png"],
        "image/png",
        PNG,
    )
    assert (pdf.query["filename"], pdf.headers["content-type"]) == (
        ["claim form.pdf"],
        "application/pdf",
    )
    [item] = fake.events
    assert item.attachments == ["att-1", "att-2"]
    assert fake.agents.uploads["att-1"].event_id == item.event_id


async def test_a_new_chat_is_a_handle_on_its_thread(igni: IgniService, fake: FakeIgni) -> None:
    agent = igni.host(ORG).agent(MARIEKE)
    chat = await agent.new_chat("Claim CL-20431", thread="claim:CL-20431", text="Start here.")
    await chat.send_message("The adjuster replied.")
    await chat.queue("Check the invoice when you get to it.")
    await chat.show({"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "Paid"}}}})
    await chat.rename("Claim CL-20431 (paid)")
    await chat.cancel()

    bodies = [s.json_body() for s in fake.requests_to(IgniPaths.SERVICE_ITEMS)]
    assert [(b["kind"], b["thread"]) for b in bodies] == [("chat", "claim:CL-20431")] + [
        (kind, "claim:CL-20431") for kind in ("message", "message", "action", "action", "action")
    ]
    assert bodies[0]["title"] == "Claim CL-20431" and bodies[0]["text"] == "Start here."
    assert bodies[2]["start_run"] is False, "queued for the chat's next run"
    assert [b["action"]["op"] for b in bodies[3:]] == ["show", "rename", "cancel"]
    assert bodies[4]["action"]["title"] == "Claim CL-20431 (paid)"


async def test_a_new_chat_without_a_thread_gets_one_of_its_own(igni: IgniService) -> None:
    agent = igni.host(ORG).agent(MARIEKE)
    first = await agent.new_chat("One")
    second = await agent.new_chat("Two")
    assert first.thread.startswith("chat:") and first.thread != second.thread


async def test_an_event_is_the_extensions_and_host_send_event_is_the_same_call(
    igni: IgniService, fake: FakeIgni
) -> None:
    host = igni.host(ORG)
    await host.agent(MARIEKE).send_event("claim.created", {"id": "CL-20431"})
    await host.send_event(MARIEKE, "claim.created", {"id": "CL-20431"})

    first, second = fake.requests_to(IgniPaths.SERVICE_EVENTS)
    assert (
        first.json_body()
        == second.json_body()
        == {
            "producer": {"kind": "person", "id": "u-max"},
            "type": "claim.created",
            "payload": {"id": "CL-20431"},
        }
    )
    assert fake.requests_to(IgniPaths.SERVICE_ITEMS) == []


async def test_what_became_of_an_item_is_polled(igni: IgniService, fake: FakeIgni) -> None:
    agent = igni.host(ORG).agent(MARIEKE)
    message = await agent.send_message("hi")
    event = await agent.send_event("claim.created")

    assert (await agent.status(message.event_id)).state == "pending"
    fake.agents.settle(message.event_id, "delivered")
    fake.agents.settle(event.event_id, "not_handled")
    delivered = await agent.status(message.event_id)
    missed = await agent.status(event.event_id)

    assert (delivered.state, delivered.kind) == ("delivered", "message")
    assert (missed.state, missed.not_handled_reason) == (
        "not_handled",
        "no handler for claim.created",
    )
    with pytest.raises(EventRefused) as unknown:
        await agent.status("ev-404")
    assert unknown.value.status == 404


async def test_what_the_extension_does_not_declare_is_refused(
    igni: IgniService, fake: FakeIgni
) -> None:
    fake.agents.grants = set()
    agent = igni.host(ORG).agent(MARIEKE)

    with pytest.raises(EventRefused) as message:
        await agent.send_message("hi")
    with pytest.raises(EventRefused) as upload:
        await agent.send_message("hi", attachments=[Attachment.from_bytes("a.png", PNG)])
    await agent.send_event("claim.created")

    assert message.value.status == upload.value.status == 403
    assert "service.messages" in message.value.detail
    assert "service.attachments" in upload.value.detail
    assert [e.kind for e in fake.events] == ["event"], "a plain event needs no grant"


def test_a_mistake_is_caught_before_it_is_sent() -> None:
    with pytest.raises(ValidationError):
        Attachment.from_bytes("huge.bin", b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ValidationError):
        Attachment.from_bytes("empty.txt", b"")
    assert (
        Attachment.from_bytes("notes.unknownext", b"x").content_type == "application/octet-stream"
    )
    assert (
        json.loads(Attachment.from_bytes("a.json", b"{}").model_dump_json())["content_type"]
        == "application/json"
    )
