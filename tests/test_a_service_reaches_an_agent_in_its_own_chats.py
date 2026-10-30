"""A service's items land in the chats their threads name, never in the one on screen.

``docs/CLOUD.md`` §6.1. igni keeps a durable map from (extension, thread)
to a session: the first item of a thread opens a chat, every later one
goes there; an item with no thread goes to the extension's own chat. A
message, a chat or an action is posted as the service sent it, with no
extension code involved. Files are kept in the chat the way a person's upload is, and named as the
composer names them. A run is started, or the item waits for the run
going; an approval is never answered, and a run waiting on one is never
cancelled.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ember_code.backend.activity import ActivityState
from ember_code.backend.server_files import FilesController
from ember_code.backend.service_item_desk import HeldPostFlusher, PoolChatDesk
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.utils.mentions import process_file_mentions
from ember_code.extensions.service_events import EventHandling, ServiceEvent
from tests.service_item_rig import OPEN, Files, Inbox, item, message

CLAIM = "claim:CL-20431"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n%%EOF\n"


def _files(**extra: Any) -> dict[str, Any]:
    return {
        "attachments": [
            {"attachment_id": "a-photo", "filename": "damage.png", "content_type": "image/png"},
            {"attachment_id": "a-form", "filename": "claim.pdf", "content_type": "application/pdf"},
        ],
        **extra,
    }


# ── Threads ───────────────────────────────────────────────────────


async def test_the_first_item_of_a_thread_opens_a_chat_and_the_next_goes_there(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    await rig.deliver(message("m1", text="New claim CL-20431.", thread=CLAIM))
    await rig.deliver(message("m2", text="The adjuster replied.", thread=CLAIM))
    await rig.deliver(message("m3", text="Another claim.", thread="claim:CL-1"))

    first, second, third = rig.desk.posts
    assert first.session_id == second.session_id != third.session_id
    assert OPEN not in {first.session_id, third.session_id}
    assert rig.desk.titles[first.session_id] == "acme.claims: claim:CL-20431", "the default title"
    assert rig.threads.find("acme.claims", CLAIM).session_id == first.session_id


async def test_the_map_outlives_a_restart(database_url: DatabaseUrl):
    rig = Inbox(database_url)
    await rig.deliver(message("m1", text="one", thread=CLAIM))
    await rig.restarted().deliver([message("m2", text="two", thread=CLAIM)])

    assert len({p.session_id for p in rig.desk.posts}) == 1
    assert len(rig.desk.titles) == 2, "the chat on screen, and one for the thread"


async def test_two_items_of_a_new_thread_in_one_batch_open_one_chat(database_url: DatabaseUrl):
    rig = Inbox(database_url)
    await rig.deliver(
        message("m1", text="one", thread=CLAIM), message("m2", text="two", thread=CLAIM)
    )
    assert len({p.session_id for p in rig.desk.posts}) == 1


async def test_without_a_thread_it_goes_to_the_extensions_own_chat_not_the_open_one(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    await rig.deliver(message("e1", text="one"), message("e2", text="two"))
    await rig.deliver(message("e3", text="three", extension_id="acme.other"))

    own, again, other = rig.desk.posts
    assert own.session_id == again.session_id, "one chat per extension"
    assert other.session_id not in (own.session_id, OPEN)
    assert OPEN not in {p.session_id for p in rig.desk.posts}
    assert rig.desk.titles[own.session_id] == "acme.claims"


async def test_the_service_titles_the_chat_its_thread_opens(database_url: DatabaseUrl):
    rig = Inbox(database_url)
    await rig.deliver(message("m1", text="hi", thread=CLAIM, title="Claim CL-20431"))
    await rig.deliver(message("m2", text="hi", thread=CLAIM, title="Ignored: already open"))
    assert list(rig.desk.titles.values())[1:] == ["Claim CL-20431"]


async def test_a_chat_item_opens_the_chat_and_posts_its_first_message(database_url: DatabaseUrl):
    rig = Inbox(database_url)
    await rig.deliver(item("c1", kind="chat", type="chat", payload={}, thread=CLAIM, title="CL"))
    assert rig.desk.posts == [] and "CL" in rig.desk.titles.values()
    await rig.deliver(
        item("c2", kind="chat", type="chat", payload={}, thread="claim:2", title="C2", text="Go.")
    )
    assert [p.text for p in rig.desk.posts] == ["Go."]


# ── Attachments ───────────────────────────────────────────────────


async def test_attachments_are_fetched_kept_in_the_chat_and_named_as_the_composer_names_them(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url, files=Files(**{"a-photo": PNG, "a-form": PDF}))
    await rig.deliver(message("m1", text="Assess the damage.", thread=CLAIM, **_files()))

    [post] = rig.desk.posts
    chat = post.session_id
    photo = f"/project/.igni/attachments/{chat}/damage.png"
    form = f"/project/.igni/attachments/{chat}/claim.pdf"
    assert rig.desk.files == {photo: PNG, form: PDF}
    assert post.text == f"Assess the damage.\n\n@{photo} @{form}"
    assert rig.files.fetched == [("m1", "a-photo"), ("m1", "a-form")]


async def test_a_file_igni_server_will_not_hand_over_leaves_the_item_to_come_again(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url, files=Files())
    receipt = await rig.deliver(message("m1", text="x", **_files()))
    assert receipt.failed == ["m1"] and rig.desk.posts == []


async def test_the_files_go_through_the_composers_own_upload(tmp_path: Path):
    """``PoolChatDesk.keep_file`` is ``upload_attachment``: the same files, in the same place."""
    project = tmp_path / "project"
    project.mkdir()
    session = SimpleNamespace(
        project_dir=project,
        session_id="s-claim",
        settings=SimpleNamespace(storage=SimpleNamespace(database_url=None, data_dir=tmp_path)),
    )

    class Backend:
        def upload_attachment(self, filename: str, content_base64: str) -> Any:
            return FilesController(session).upload_attachment(filename, content_base64)

    class Pool:
        async def get_or_create(self, session_id: str) -> Any:
            return SimpleNamespace(backend=Backend())

    desk = PoolChatDesk(SimpleNamespace(pool=Pool()))
    ref = await desk.keep_file(session_id="s-claim", filename="damage photo.png", content=PNG)

    kept = Path(ref)
    assert kept == project / ".igni" / "attachments" / "s-claim" / "damage_photo.png"
    assert kept.read_bytes() == PNG
    composed, referenced = process_file_mentions(f"Assess it.\n\n@{ref}")
    assert referenced == [ref] and "<attached-files>" in composed


# ── As sent ───────────────────────────────────────────────────


async def test_a_message_is_posted_as_sent_and_never_offered_to_the_extension(
    database_url: DatabaseUrl,
):
    handed: list[str] = []

    async def hand(event: ServiceEvent) -> EventHandling:
        handed.append(event.event_id)
        return EventHandling(handled=True)

    rig = Inbox(database_url, hand=hand)
    await rig.deliver(message("m1", text="Exactly this.", thread=CLAIM))
    [post] = rig.desk.posts
    assert handed == [] and post.text == "Exactly this."
    assert post.from_service == "svc-claims", "marked as the service's"


async def test_an_action_shows_a_view_that_never_reaches_the_model(database_url: DatabaseUrl):
    asked: list[str] = []

    async def hand(event: ServiceEvent) -> EventHandling:
        asked.append(event.event_id)
        return EventHandling(handled=True)

    rig = Inbox(database_url, hand=hand)
    view = {"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "Paid"}}}}
    await rig.deliver(
        item(
            "a1",
            kind="action",
            type="action",
            payload={},
            thread=CLAIM,
            action={"op": "show", "view": view},
        )
    )
    assert asked == []
    [(chat, view_id, shown)] = rig.desk.views
    assert shown == view and view_id == "svc-a1" and rig.desk.posts == [], "never sent to the model"


# ── Starting a run, or waiting for one ────────────────────────────


async def test_start_run_false_waits_for_the_chats_next_run(database_url: DatabaseUrl):
    rig = Inbox(database_url)
    await rig.deliver(message("m1", text="for later", start_run=False))
    assert [(p.next_run, p.text) for p in rig.desk.posts] == [(True, "for later")]


async def test_files_arriving_during_a_run_are_held_until_it_ends(database_url: DatabaseUrl):
    rig = Inbox(database_url, files=Files(**{"a-photo": PNG, "a-form": PDF}))
    await rig.deliver(message("m0", text="open the chat", thread=CLAIM))
    chat = rig.desk.posts[0].session_id
    rig.desk.running.add(chat)

    receipt = await rig.deliver(message("m1", text="photos", thread=CLAIM, **_files()))
    flusher = HeldPostFlusher(held=rig.held, desk=rig.desk)  # type: ignore[arg-type]
    assert receipt.delivered == ["m1"], "held durably counts as delivered"
    assert await flusher.flush() == 0 and len(rig.desk.posts) == 1, "not into the running run"

    rig.desk.running.clear()
    assert await flusher.flush() == 1
    held = rig.desk.posts[-1]
    assert held.session_id == chat and not held.queued and "@" in held.text
    assert rig.held.waiting() == []


async def test_an_approval_is_never_answered_and_its_run_never_cancelled(
    database_url: DatabaseUrl,
):
    rig = Inbox(database_url)
    await rig.deliver(message("m0", text="start", thread=CLAIM))
    chat = rig.desk.posts[0].session_id
    rig.desk.waiting.add(chat)

    await rig.deliver(message("m1", text="more", thread=CLAIM))
    await rig.deliver(
        item("a1", kind="action", type="action", payload={}, thread=CLAIM, action={"op": "cancel"})
    )
    assert rig.desk.posts[-1].queued, "waits for the run the person is deciding in"
    assert rig.desk.cancelled == []

    rig.desk.waiting.clear()
    rig.desk.running.add(chat)
    await rig.deliver(
        item("a2", kind="action", type="action", payload={}, thread=CLAIM, action={"op": "cancel"})
    )
    assert rig.desk.cancelled == [chat], "a working run may be cancelled"


async def test_rename_retitles_the_threads_chat(database_url: DatabaseUrl):
    rig = Inbox(database_url)
    await rig.deliver(message("m0", text="start", thread=CLAIM))
    await rig.deliver(
        item(
            "a1",
            kind="action",
            type="action",
            payload={},
            thread=CLAIM,
            action={"op": "rename", "title": "CL-20431 (paid)"},
        )
    )
    chat = rig.desk.posts[0].session_id
    assert rig.desk.titles[chat] == "CL-20431 (paid)"
    assert rig.threads.find("acme.claims", CLAIM).title == "CL-20431 (paid)"


async def test_the_pool_desk_knows_an_approval_from_a_working_run():
    state = [ActivityState.RUNNING]

    class Pool:
        def find(self, session_id: str) -> Any:
            return SimpleNamespace(
                backend=SimpleNamespace(activity=lambda: SimpleNamespace(state=state[0]))
            )

    desk = PoolChatDesk(SimpleNamespace(pool=Pool()))
    assert desk.busy("s") and not desk.waiting_on_approval("s")
    state[0] = ActivityState.WAITING
    assert desk.busy("s") and desk.waiting_on_approval("s")
