"""An extension writes to any session's chat without moving what the person sees.

``docs/HOST_OBJECT_MODEL.md``: ``host.sessions.chat(id)`` is ``host.chat``
for any session of this project. Its ``post`` follows a service event's
rules (``extensions/service_events.py``, step 3): in a session where
nothing is going on it starts a run; while a run is going — working, or
waiting on an approval — it is queued for that run, and never answers
the approval. No window moves. Writing anywhere but the session on
screen needs ``sessions.post`` as well as the member's own grant; a
session that is not this project's is refused, and an id that is no
session is not created. Named by its id, the session on screen is
``host.chat`` exactly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember_code.backend.activity import ActivityState, SessionActivity
from ember_code.protocol import messages as msg
from tests.host_model_helpers import FakeSession, HostRig

WRITER = ["chat.read", "chat.post", "chat.append", "sessions.post"]
SPEC = {"root": "c", "elements": {"c": {"type": "Card", "props": {"title": "Indexed"}}}}


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


def refused(reply: Any, code: str) -> str:
    assert not reply.ok, f"answered {reply.result!r}"
    assert reply.error.code == code, reply.error
    return reply.error.message


async def background(rig: HostRig, session_id: str = "s-2") -> FakeSession:
    """A second session of the project, live and not on screen."""
    assert rig.orchestrator is not None
    await rig.orchestrator.attach(session_id, None)
    runtime = rig.orchestrator.pool.find(session_id)
    assert runtime is not None
    backend: FakeSession = runtime.backend
    return backend


def doing(session: FakeSession, state: ActivityState, *, waiting: int = 0) -> None:
    session.activity = lambda: SessionActivity(  # type: ignore[attr-defined]
        session_id=session.session_id, state=state, waiting_approvals=waiting
    )


def echoes(rig: HostRig, session_id: str) -> list[Any]:
    return [m for m in rig.windows.of(msg.UserMessageReceived) if m.session_id == session_id]


class TestPostingWhereNobodyIsLooking:
    async def test_it_starts_a_run_there_and_the_window_stays_where_it_was(
        self, rig: HostRig
    ) -> None:
        window = await rig.window("w-main", session="boot")
        other = await background(rig)
        ext = rig.install(host=WRITER)
        moved_before = [p for p in await rig.window_pushes(window) if p["field"] == "session"]

        ok(await rig.ask(ext, "call", "chat.post", session="s-2", text="The index is ready."))
        await rig.settle()

        [(agent_text, _)] = other.ran
        assert f'<extension-message from="{ext}">' in agent_text
        assert "The index is ready." in agent_text
        assert rig.session.ran == [], "the session on screen was not touched"
        [echo] = echoes(rig, "s-2")
        assert echo.extension_id == ext and not echo.queued, "labelled as the extension's"
        assert echoes(rig, "boot") == []
        # The run streamed stamped with its own session, which windows
        # showing another one do not draw.
        assert all(m.session_id == "s-2" for m in rig.windows.of(msg.RunStarted))
        assert rig.model is not None
        assert rig.model.windows.session(window) == "boot"
        moved = [p for p in await rig.window_pushes(window) if p["field"] == "session"]
        assert moved == moved_before, "no window was moved"

    async def test_while_a_run_works_there_it_is_queued_for_that_run(self, rig: HostRig) -> None:
        other = await background(rig)
        doing(other, ActivityState.RUNNING)
        ext = rig.install(host=WRITER)

        ok(await rig.ask(ext, "call", "chat.post", session="s-2", text="Re-check auth.py."))
        await rig.settle()

        assert other.ran == [], "not a second run beside the one going"
        assert other.runtime.queue[-1].endswith("Re-check auth.py.\n</extension-message>")
        [echo] = echoes(rig, "s-2")
        assert echo.queued and echo.extension_id == ext

    async def test_a_waiting_approval_is_not_answered(self, rig: HostRig) -> None:
        other = await background(rig)
        doing(other, ActivityState.WAITING, waiting=1)
        ext = rig.install(host=WRITER)

        ok(await rig.ask(ext, "call", "chat.post", session="s-2", text="yes"))
        await rig.settle()

        assert other.answers == [], "the approval is the person's"
        assert other.ran == []
        assert other.runtime.queue[-1].endswith("yes\n</extension-message>")

    async def test_attachments_cannot_wait_for_a_run_and_are_refused(self, rig: HostRig) -> None:
        other = await background(rig)
        doing(other, ActivityState.RUNNING)
        ext = rig.install(host=WRITER)

        message = refused(
            await rig.ask(
                ext,
                "call",
                "chat.post",
                session="s-2",
                text="see this",
                attachments={"a.txt": "x"},
            ),
            "refused",
        )
        assert "attachments" in message
        assert other.runtime.queue == [] and other.ran == []

    async def test_queue_and_append_write_there(self, rig: HostRig) -> None:
        other = await background(rig)
        ext = rig.install(host=WRITER)

        ok(await rig.ask(ext, "call", "chat.queue", session="s-2", text="later"))
        view_id = ok(await rig.ask(ext, "call", "chat.append", session="s-2", view=SPEC))
        ok(
            await rig.ask(
                ext, "call", "chat.replace", session="s-2", view_id=view_id, view=SPEC, title="2"
            )
        )
        await rig.settle()

        assert other.runtime.queue[-1].endswith("later\n</extension-message>")
        kept = [e.payload["spec_id"] for e in other.session_events("visualization_delta")]
        assert kept == [view_id, view_id]
        assert rig.session.session_events("visualization_delta") == []
        assert [v["view_id"] for v in ok(await rig.ask(ext, "get", "chat.views.list"))] == []

    async def test_its_transcript_reads_there(self, rig: HostRig) -> None:
        other = await background(rig)
        other.ran.append(("an earlier turn", {}))
        ext = rig.install(host=["chat.read"])

        assert ok(await rig.ask(ext, "get", "chat.session_id", session="s-2")) == "s-2"
        messages = ok(await rig.ask(ext, "get", "chat.messages", session="s-2"))
        assert messages == [{"role": "user", "content": "an earlier turn"}]
        assert ok(await rig.ask(ext, "get", "chat.messages")) == [], "host.chat is unchanged"

    async def test_a_session_in_the_list_but_not_loaded_is_reached(self, rig: HostRig) -> None:
        async def listed() -> msg.SessionListResult:
            return msg.SessionListResult(
                sessions=[
                    msg.SessionListEntry(session_id="boot", name="First", run_count=1),
                    msg.SessionListEntry(session_id="s-old", name="Yesterday", run_count=4),
                ]
            )

        rig.session.list_sessions = listed  # type: ignore[method-assign]
        ext = rig.install(host=WRITER)

        ok(await rig.ask(ext, "call", "chat.post", session="s-old", text="hello"))
        await rig.settle()

        assert rig.orchestrator is not None
        runtime = rig.orchestrator.pool.find("s-old")
        assert runtime is not None and len(runtime.backend.ran) == 1


class TestWhatIsRefused:
    async def test_an_unknown_session_is_not_found_and_not_created(self, rig: HostRig) -> None:
        ext = rig.install(host=WRITER)

        message = refused(
            await rig.ask(ext, "call", "chat.post", session="nope", text="hi"), "not_found"
        )
        assert "nope" in message
        refused(await rig.ask(ext, "get", "chat.messages", session="nope"), "not_found")
        assert rig.orchestrator is not None
        assert rig.orchestrator.pool.find("nope") is None

    async def test_another_projects_session_is_refused(self, rig: HostRig, tmp_path: Path) -> None:
        elsewhere = tmp_path / "another-project"
        elsewhere.mkdir()
        assert rig.orchestrator is not None
        await rig.orchestrator.attach("theirs", str(elsewhere))
        ext = rig.install(host=WRITER)

        message = refused(
            await rig.ask(ext, "call", "chat.post", session="theirs", text="hi"), "refused"
        )
        assert "different project" in message
        theirs = rig.orchestrator.pool.find("theirs")
        assert theirs is not None and theirs.backend.ran == []

    async def test_writing_elsewhere_needs_sessions_post(self, rig: HostRig) -> None:
        other = await background(rig)
        ext = rig.install(host=["chat.post", "chat.append"])

        message = refused(
            await rig.ask(ext, "call", "chat.post", session="s-2", text="hi"), "not_granted"
        )
        assert "'sessions.post'" in message
        refused(await rig.ask(ext, "call", "chat.queue", session="s-2", text="hi"), "not_granted")
        refused(await rig.ask(ext, "call", "chat.append", session="s-2", view=SPEC), "not_granted")
        await rig.settle()
        assert other.ran == [] and other.runtime.queue == []

    async def test_sessions_post_does_not_stand_in_for_the_members_own_grant(
        self, rig: HostRig
    ) -> None:
        other = await background(rig)
        ext = rig.install(host=["sessions.post"])

        message = refused(
            await rig.ask(ext, "call", "chat.post", session="s-2", text="hi"), "not_granted"
        )
        assert "'chat.post'" in message
        await rig.settle()
        assert other.ran == []

    async def test_chat_wildcard_does_not_grant_writing_elsewhere(self, rig: HostRig) -> None:
        await background(rig)
        ext = rig.install(host=["chat.*"])

        refused(await rig.ask(ext, "call", "chat.post", session="s-2", text="hi"), "not_granted")

    async def test_steering_and_answering_act_only_on_the_session_on_screen(
        self, rig: HostRig
    ) -> None:
        other = await background(rig)
        ext = rig.install(host=["chat.*", "sessions.post"])

        refused(await rig.ask(ext, "call", "chat.cancel", session="s-2"), "refused")
        refused(
            await rig.ask(
                ext,
                "call",
                "chat.approvals.answer",
                session="s-2",
                requirement_id="r-1",
                action="confirm",
            ),
            "refused",
        )
        assert other.answers == []

    async def test_a_session_on_an_object_no_session_owns_is_invalid(self, rig: HostRig) -> None:
        await background(rig)
        ext = rig.install(host=["app"])

        refused(await rig.ask(ext, "get", "app.version", session="s-2"), "invalid")


class TestTheSessionOnScreenIsHostChat:
    async def test_named_by_its_id_it_is_host_chat_with_host_chats_grants(
        self, rig: HostRig
    ) -> None:
        await background(rig)
        ext = rig.install(host=["chat.post"])

        ok(await rig.ask(ext, "call", "chat.post", session="boot", text="first"))
        ok(await rig.ask(ext, "call", "chat.post", text="second"))
        await rig.settle()

        assert [text.count("first") + text.count("second") for text, _ in rig.session.ran] == [
            1,
            1,
        ]
        assert len(echoes(rig, "boot")) == 2

    async def test_a_window_on_another_session_makes_that_one_host_chat(self, rig: HostRig) -> None:
        other = await background(rig)
        await rig.window("w-main", session="s-2")
        ext = rig.install(host=["chat.post"])

        ok(await rig.ask(ext, "call", "chat.post", session="s-2", text="here"))
        await rig.settle()

        assert len(other.ran) == 1
        refused(await rig.ask(ext, "call", "chat.post", session="boot", text="x"), "not_granted")
