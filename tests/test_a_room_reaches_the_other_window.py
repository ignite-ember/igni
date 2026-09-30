"""Two windows on one machine are two people in the room.

One of these processes holds one socket per document, however many
windows are open on it, and the server relays a change to every
socket **except** the one that sent it. That is right — a client has
already applied what it typed — and it is also the reason every
window on this machine is invisible to every other unless this
process says so itself.

So whatever goes out on the wire goes back to the local bus too.
Changes did. Carets did not, and the result was the failure that is
hardest to argue with: two windows editing one document, text
flowing between them, and no sign of who was typing it.

The other half is a document nobody has shared. It has no socket at
all — there is no room on any server to relay through — so the bus
is not a supplement here, it is the whole transport. A caret that is
only echoed for shared documents is a caret that never appears on
most of them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.backend.server_artifacts import ArtifactController, OpenRoom


class Bus:
    """The broadcast bus, remembering what was put on it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def emit(self, event) -> None:
        self.sent.append((event.channel, event.payload))

    def frames(self) -> list[dict]:
        return [p["frame"] for c, p in self.sent if c == "artifact_draft" and "frame" in p]

    def kinds(self) -> list[str]:
        return [str(f.get("type") or "") for f in self.frames()]


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def controller(bus) -> ArtifactController:
    session = SimpleNamespace(artifacts=SimpleNamespace(), broadcast_bus=bus)
    return ArtifactController(session)


def local_room(controller: ArtifactController, artifact_id: str = "doc-1") -> OpenRoom:
    """A document nobody has shared: no outbox, because no socket."""
    room = OpenRoom(artifact_id=artifact_id, watchers={"w1"})
    controller._rooms[artifact_id] = room
    return room


def shared_room(controller: ArtifactController, artifact_id: str = "doc-1") -> OpenRoom:
    """A document with a shared copy: frames go out as well as round."""
    import asyncio

    room = OpenRoom(artifact_id=artifact_id, watchers={"w1"}, outbox=asyncio.Queue())
    controller._rooms[artifact_id] = room
    return room


class TestWhatReachesTheOtherWindow:
    async def test_a_change_does(self, controller, bus):
        local_room(controller)

        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "edit", "update": "abc"}
        )

        assert bus.kinds() == ["edit"]

    async def test_a_caret_does(self, controller, bus):
        # The one that was missing. Without it two windows edit one
        # document and show each other no cursor at all.
        local_room(controller)

        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "cursor", "update": "xyz"}
        )

        assert bus.kinds() == ["cursor"]

    async def test_a_caret_does_on_a_shared_document_too(self, controller, bus):
        # Shared or not, the windows here are behind one socket and
        # the server does not echo to it.
        room = shared_room(controller)

        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "cursor", "update": "xyz"}
        )

        assert bus.kinds() == ["cursor"]
        assert room.outbox.qsize() == 1, "and it still goes out to everybody else"

    async def test_a_frame_for_another_document_does_not(self, controller, bus):
        # A change typed into one document must never be relayed into
        # another.
        local_room(controller, "doc-1")

        answer = await controller.send_to_room(
            artifact_id="doc-2", frame={"type": "edit", "update": "abc"}
        )

        assert answer.ok is False
        assert bus.frames() == []


class TestWhatIsWrittenDown:
    async def test_a_change_is_kept_for_whoever_opens_next(self, controller):
        local_room(controller)

        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "edit", "update": "abc"}
        )

        state = await controller.room_state(artifact_id="doc-1")
        assert state.updates == ["abc"]

    async def test_a_caret_is_not(self, controller):
        # A caret is true while somebody is looking at it and
        # meaningless afterwards. Kept in the log it would be handed
        # to the next window to open as though it were a change to
        # the document — in an encoding that is not one.
        local_room(controller)

        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "cursor", "update": "xyz"}
        )

        state = await controller.room_state(artifact_id="doc-1")
        assert state.updates == []

    async def test_a_snapshot_replaces_the_log(self, controller):
        local_room(controller)
        for update in ("a", "b", "c"):
            await controller.send_to_room(
                artifact_id="doc-1", frame={"type": "edit", "update": update}
            )

        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "snapshot", "update": "whole"}
        )

        state = await controller.room_state(artifact_id="doc-1")
        assert state.updates == ["whole"]

    async def test_a_local_log_outlives_the_room(self, controller):
        # There is no server holding one for these, so this process
        # has to — otherwise closing the last window on a document
        # loses what was typed into it since the last save.
        local_room(controller)
        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "edit", "update": "abc"}
        )

        await controller.leave_room(artifact_id="doc-1", watcher="w1")

        state = await controller.room_state(artifact_id="doc-1")
        assert state.updates == ["abc"]

    async def test_publishing_forgets_it(self, controller):
        local_room(controller)
        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "edit", "update": "abc"}
        )

        controller.forget_local_draft("doc-1")

        state = await controller.room_state(artifact_id="doc-1")
        assert state.updates == []


class TestWhoIsWatching:
    async def test_the_room_stays_while_anybody_is(self, controller):
        # One window navigating away used to close the room another
        # window was still editing in.
        room = local_room(controller)
        room.watchers.add("w2")

        await controller.leave_room(artifact_id="doc-1", watcher="w1")

        assert "doc-1" in controller._rooms

    async def test_and_goes_when_the_last_one_leaves(self, controller):
        room = local_room(controller)
        room.watchers.add("w2")

        await controller.leave_room(artifact_id="doc-1", watcher="w1")
        await controller.leave_room(artifact_id="doc-1", watcher="w2")

        assert "doc-1" not in controller._rooms

    async def test_a_second_window_gets_the_log_in_its_answer(self, controller):
        # Not on a broadcast it has to wait for: the server sends its
        # log once, on connecting, which is long before a window that
        # opens the editor later starts listening.
        local_room(controller)
        await controller.send_to_room(
            artifact_id="doc-1", frame={"type": "edit", "update": "abc"}
        )

        joined = await controller.watch_room(artifact_id="doc-1", watcher="w2")

        assert joined.updates == ["abc"]
