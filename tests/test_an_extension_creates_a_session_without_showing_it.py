"""An extension opens a titled session for its own work, and no window moves.

``docs/HOST_OBJECT_MODEL.md``: ``host.sessions.create(project_dir, title,
focus)`` starts a session and returns it (``SessionAttached``), reachable
with ``host.sessions.chat(session_id)``. It moves no window unless
``focus`` is true. A ``title`` is kept as a service's chat title is
(``BackendServer.title_session``), before anything runs in it, so no
auto-name replaces it, and every window is told (``session_named``). It
needs the ``sessions`` grant, which already lets an extension create a
session and switch the window to it — a session nobody is shown asks for
no more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember_code.protocol import messages as msg
from tests.host_model_helpers import FakeSession, HostRig

WRITER = ["sessions", "chat.read", "chat.post", "sessions.post"]


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


def session(rig: HostRig, session_id: str) -> FakeSession:
    assert rig.orchestrator is not None
    runtime = rig.orchestrator.pool.find(session_id)
    assert runtime is not None, f"{session_id} is not live"
    backend: FakeSession = runtime.backend
    return backend


async def session_moves(rig: HostRig, window: str) -> list[dict[str, Any]]:
    return [p for p in await rig.window_pushes(window) if p["field"] == "session"]


async def test_a_titled_session_is_created_and_the_window_stays(rig: HostRig) -> None:
    window = await rig.window("w-main", session="boot")
    ext = rig.install(host=WRITER)
    before = await session_moves(rig, window)

    created = ok(await rig.ask(ext, "call", "sessions.create", title="Index: acme/api"))
    await rig.settle()

    new_id = created["session_id"]
    assert new_id and new_id != "boot"
    assert created["project_dir"] == str(rig.project)
    assert session(rig, new_id).titles == ["Index: acme/api"]
    named = [
        p.payload for p in rig.windows.pushes("session_named") if p.payload["session_id"] == new_id
    ]
    assert named == [{"session_id": new_id, "name": "Index: acme/api"}]
    assert rig.model is not None
    assert rig.model.windows.session(window) == "boot"
    assert await session_moves(rig, window) == before, "no window was moved"


async def test_its_chat_is_reached_by_its_id_and_a_post_starts_a_run_there(
    rig: HostRig,
) -> None:
    await rig.window("w-main", session="boot")
    ext = rig.install(host=WRITER)

    created = ok(await rig.ask(ext, "call", "sessions.create", title="Index"))
    ok(
        await rig.ask(
            ext, "call", "chat.post", session=created["session_id"], text="The index is ready."
        )
    )
    await rig.settle()

    [(agent_text, _)] = session(rig, created["session_id"]).ran
    assert "The index is ready." in agent_text
    assert rig.session.ran == [], "the session on screen was not touched"
    assert {m.session_id for m in rig.windows.of(msg.RunStarted)} == {created["session_id"]}


async def test_without_a_title_it_is_not_named(rig: HostRig) -> None:
    ext = rig.install(host=["sessions"])

    created = ok(await rig.ask(ext, "call", "sessions.create"))
    await rig.settle()

    assert session(rig, created["session_id"]).titles == []
    assert rig.windows.pushes("session_named") == []


async def test_focus_shows_it_in_the_callers_window(rig: HostRig) -> None:
    window = await rig.window("w-main", session="boot")
    ext = rig.install(host=["sessions"])

    created = ok(await rig.ask(ext, "call", "sessions.create", title="Review", focus=True))
    await rig.settle()

    assert rig.model is not None
    assert rig.model.windows.session(window) == created["session_id"]
    assert session(rig, created["session_id"]).titles == ["Review"]


async def test_it_is_refused_without_the_sessions_grant(rig: HostRig) -> None:
    ext = rig.install(host=["chat.read", "chat.post", "sessions.post"])
    assert rig.orchestrator is not None
    live = len(rig.orchestrator.pool.runtimes)

    reply = await rig.ask(ext, "call", "sessions.create", title="Index")

    assert not reply.ok and reply.error.code == "not_granted"
    assert "'sessions'" in reply.error.message
    assert len(rig.orchestrator.pool.runtimes) == live, "no session was created"


async def test_an_empty_title_is_invalid(rig: HostRig) -> None:
    ext = rig.install(host=["sessions"])
    assert rig.orchestrator is not None
    live = len(rig.orchestrator.pool.runtimes)

    reply = await rig.ask(ext, "call", "sessions.create", title="  ")

    assert not reply.ok and reply.error.code == "invalid"
    assert len(rig.orchestrator.pool.runtimes) == live
