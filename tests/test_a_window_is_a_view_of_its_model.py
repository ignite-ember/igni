"""A window is a view of a model the host owns.

``docs/HOST_OBJECT_MODEL.md``, *The UI is part of the model*: everything
a window shows is a field of its model (:mod:`ember_code.extensions.host.windows`),
the window's views bind to it, and they write to it by the same path an
extension's ``host.ui`` does. So a write from an extension reaches the
window (the ``window_model_changed`` push it applies), and a write from
the window is a signal — whoever made it, the same change.

These drive both ends over the backend as the app assembles it
(:class:`tests.host_model_helpers.HostRig`): the window through the
``window_model`` RPC the web client sends, the extension through the
host model's requests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.host.windows import references
from tests.host_model_helpers import HostRig, Sink


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


def listen(rig: HostRig, extension_id: str, *wires: str) -> Sink:
    assert rig.model is not None
    sink = Sink()
    rig.model.bus.subscribe(rig.model.bus.attach(extension_id, sink), list(wires))
    return sink


class TestAWindowHydratesFromTheModel:
    async def test_the_whole_model_and_its_revision(self, rig: HostRig) -> None:
        answer = await rig.window_model("w-1", "hydrate", connection_id="ws-1", kind="web")
        state = answer["state"]
        assert set(state) == {
            "session",
            "project_dir",
            "route",
            "panel",
            "sidebar",
            "preview",
            "selection",
            "composer",
            "toasts",
            "dialog",
            "scroll",
            "focused",
        }
        assert state["sidebar"] == {"open": False, "width": None, "section": None}
        assert state["composer"] == {"text": "", "attachments": [], "mode": "chat"}
        assert state["scroll"] == {"message_id": None, "at_bottom": True}
        assert answer["rev"] == 0

    async def test_what_lasts_is_there_after_a_restart(self, rig: HostRig) -> None:
        await rig.window("w-1", session="boot")
        await rig.window_writes("w-1", "route", [{"kind": "plugins", "label": "Plugins"}])
        await rig.window_writes(
            "w-1", "sidebar", {"open": True, "width": 300, "section": "sessions"}
        )
        await rig.window_writes("w-1", "composer", {"text": "half a thought", "mode": "shell"})
        await rig.window_writes("w-1", "panel", {"kind": "notifications"})
        # A second backend over the same store: what lasts is read back,
        # what lives in the process (the panel) does not.
        again = await HostRig(rig.tmp).start()
        try:
            state = (await again.window_model("w-1", "hydrate"))["state"]
        finally:
            await again.stop()
        assert state["route"] == [{"kind": "plugins", "label": "Plugins", "params": {}}]
        assert state["sidebar"] == {"open": True, "width": 300, "section": "sessions"}
        assert state["composer"] == {"text": "half a thought", "attachments": [], "mode": "shell"}
        assert state["panel"] is None


class TestAnExtensionsWriteReachesTheWindow:
    """The view binds to the model: what an extension writes is pushed to
    the window it is about, which applies it as its own."""

    async def test_every_writable_field(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"], workspace="read")
        await rig.window("w-1", session="boot")
        (rig.project / "README.md").write_text("hello")
        ok(await rig.ask(ext, "call", "ui.open_panel", kind="notifications"))
        ok(await rig.ask(ext, "call", "ui.open_preview", path="README.md"))
        ok(await rig.ask(ext, "call", "ui.select", selection={"message_id": "run-1"}))
        ok(await rig.ask(ext, "call", "ui.set_sidebar", open=True, width=320, section="links"))
        ok(await rig.ask(ext, "call", "ui.set_composer", text="look at", attachments=["a.py"]))
        ok(await rig.ask(ext, "call", "ui.scroll_to", message_id="run-1"))
        pushed = {p["field"]: p for p in (await rig.window_pushes("w-1"))}
        assert pushed["panel"]["value"] == {"kind": "notifications", "params": {}}
        assert pushed["preview"]["value"] == {"path": "README.md"}
        assert pushed["selection"]["value"] == {"message_id": "run-1", "text": None, "file": None}
        assert pushed["sidebar"]["value"] == {"open": True, "width": 320, "section": "links"}
        assert pushed["composer"]["value"] == {
            "text": "look at @a.py ",
            "attachments": ["a.py"],
            "mode": "chat",
        }
        assert pushed["scroll"]["value"] == {"message_id": "run-1", "at_bottom": False}
        # Nobody's echo: an extension's write carries no window's token,
        # so the window applies it rather than taking it for its own.
        assert {p["token"] for p in pushed.values()} == {""}
        # And the window's next hydrate reads the same.
        state = (await rig.window_model("w-1", "hydrate"))["state"]
        assert state["preview"] == {"path": "README.md"}

    async def test_properties_are_writable_as_qml_properties_are(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        ok(await rig.ask(ext, "set", "ui.panel", to={"kind": "search"}))
        assert ok(await rig.ask(ext, "get", "ui.panel")) == {"kind": "search", "params": {}}
        ok(await rig.ask(ext, "set", "ui.panel", to=None))
        assert ok(await rig.ask(ext, "get", "ui.panel")) is None
        ok(await rig.ask(ext, "set", "ui.selection", to={"text": "this line"}))
        assert ok(await rig.ask(ext, "get", "ui.selection"))["text"] == "this line"

    async def test_a_write_that_does_not_fit_is_refused_and_changes_nothing(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        refused(await rig.ask(ext, "call", "ui.open_panel", kind="cupboard"), "invalid")
        refused(await rig.ask(ext, "call", "ui.set_sidebar", open=True, width=5000), "invalid")
        refused(await rig.ask(ext, "call", "ui.set_sidebar", open=True, section="attic"), "invalid")
        refused(await rig.ask(ext, "call", "ui.set_composer", text="x", mode="loud"), "invalid")
        assert (await rig.window_pushes("w-1")) == []

    async def test_a_preview_is_of_what_the_workspace_grant_reaches(self, rig: HostRig) -> None:
        blind = rig.install("acme.blind", host=["ui"])
        docs = rig.install("acme.docs", host=["ui"], workspace="read", workspacePaths=["docs/**"])
        await rig.window("w-1")
        refused(await rig.ask(blind, "call", "ui.open_preview", path="README.md"), "refused")
        refused(await rig.ask(docs, "call", "ui.open_preview", path="src/secret.py"), "refused")
        ok(await rig.ask(docs, "call", "ui.open_preview", path="docs/guide.md"))


class TestWhatTheWindowDoesIsASignal:
    async def test_each_field_the_view_writes(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        sink = listen(
            rig,
            ext,
            "ui.panel_changed",
            "ui.preview_changed",
            "ui.selection_changed",
            "ui.scrolled",
            "ui.composer_changed",
            "ui.sidebar_changed",
            "changed",
        )
        await rig.window("w-1", session="boot")
        await rig.window_writes("w-1", "panel", {"kind": "search"}, token="t1")
        await rig.window_writes("w-1", "preview", {"path": "a.py"}, token="t2")
        await rig.window_writes("w-1", "selection", {"text": "two words"}, token="t3")
        await rig.window_writes("w-1", "scroll", {"message_id": "run-4", "at_bottom": False})
        await rig.window_writes("w-1", "composer", {"text": "/hel", "mode": "chat"})
        await rig.window_writes("w-1", "sidebar", {"open": True, "section": "sessions"})
        assert sink.values("ui.panel_changed") == [
            {"window": "w-1", "panel": {"kind": "search", "params": {}}}
        ]
        assert sink.values("ui.preview_changed") == [{"window": "w-1", "preview": {"path": "a.py"}}]
        assert sink.values("ui.selection_changed")[0]["selection"]["text"] == "two words"
        assert sink.values("ui.scrolled") == [
            {"message_id": "run-4", "at_bottom": False, "window": "w-1"}
        ]
        assert sink.values("ui.composer_changed")[0]["composer"]["text"] == "/hel"
        assert sink.values("ui.sidebar_changed")[0]["sidebar"]["section"] == "sessions"
        paths = {c["path"] for c in sink.values("changed")}
        assert {
            "ui.panel",
            "ui.preview",
            "ui.selection",
            "ui.scroll",
            "ui.composer",
            "ui.sidebar",
        } <= paths
        # The window's own token comes back on the push, so it knows its echo.
        assert [p["token"] for p in (await rig.window_pushes("w-1"))[:3]] == ["t1", "t2", "t3"]

    async def test_a_write_the_model_refuses_is_an_error_the_window_sees(
        self, rig: HostRig
    ) -> None:
        await rig.window("w-1")
        assert "no field" in await rig.window_writes("w-1", "wallpaper", "blue")
        assert "expected" in await rig.window_writes("w-1", "sidebar", {"open": "maybe"})
        assert "own operations" in await rig.window_writes("w-1", "toasts", [])


class TestOrderIsTheModels:
    async def test_every_change_has_the_next_revision_and_the_last_write_wins(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        await rig.window_writes("w-1", "panel", {"kind": "search"}, token="mine")
        ok(await rig.ask(ext, "call", "ui.open_panel", kind="notifications"))
        revs = [p["rev"] for p in (await rig.window_pushes("w-1"))]
        assert revs == sorted(revs) and len(set(revs)) == len(revs)
        state = (await rig.window_model("w-1", "hydrate"))["state"]
        assert state["panel"]["kind"] == "notifications"


class TestToasts:
    async def test_an_extensions_toast_is_shown_and_the_window_dismisses_it(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        sink = listen(rig, ext, "ui.toast_shown", "ui.toast_dismissed")
        await rig.window("w-1")
        toast = ok(
            await rig.ask(
                ext,
                "call",
                "ui.toast",
                text="Indexed",
                kind="success",
                target={"kind": "codeindex", "label": "CodeIndex"},
            )
        )
        assert toast["source"] == ext and toast["kind"] == "success"
        [shown] = [p for p in (await rig.window_pushes("w-1")) if p["event"] == "toast_shown"]
        assert shown["item"]["id"] == toast["id"]
        assert ok(await rig.ask(ext, "get", "ui.toasts"))[0]["text"] == "Indexed"
        await rig.window_model("w-1", "dismiss_toast", value=toast["id"])
        assert ok(await rig.ask(ext, "get", "ui.toasts")) == []
        assert [v["toast"]["id"] for v in sink.values("ui.toast_shown")] == [toast["id"]]
        assert [v["toast"]["id"] for v in sink.values("ui.toast_dismissed")] == [toast["id"]]

    async def test_igni_s_own_toast_is_a_signal_too(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        sink = listen(rig, ext, "ui.toast_shown")
        await rig.window("w-1")
        await rig.window_model("w-1", "toast", value={"text": "Task done", "kind": "info"})
        [shown] = sink.values("ui.toast_shown")
        assert shown["toast"]["text"] == "Task done" and shown["toast"]["source"] is None

    async def test_an_extension_dismisses_only_its_own(self, rig: HostRig) -> None:
        one = rig.install("acme.one", host=["ui"])
        two = rig.install("acme.two", host=["ui"])
        await rig.window("w-1")
        toast = ok(await rig.ask(one, "call", "ui.toast", text="mine"))
        refused(await rig.ask(two, "call", "ui.dismiss_toast", toast_id=toast["id"]), "refused")
        ok(await rig.ask(one, "call", "ui.dismiss_toast", toast_id=toast["id"]))
        refused(await rig.ask(one, "call", "ui.dismiss_toast", toast_id=toast["id"]), "not_found")


class TestDialogs:
    async def answered_by_the_window(
        self, rig: HostRig, ext: str, spec: dict[str, Any], answer: Any
    ) -> tuple[Any, dict[str, Any]]:
        asked = asyncio.create_task(rig.ask(ext, "call", "ui.show_dialog", spec=spec))
        await rig.settle()
        [opened] = [p for p in (await rig.window_pushes("w-1")) if p["event"] == "dialog_opened"][
            -1:
        ]
        dialog = opened["item"]
        await rig.window_model("w-1", "answer_dialog", dialog_id=dialog["id"], value=answer)
        return await asyncio.wait_for(asked, 5), dialog

    async def test_show_dialog_returns_the_persons_answer(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        sink = listen(rig, ext, "ui.dialog_opened", "ui.dialog_closed")
        await rig.window("w-1")
        spec = {
            "title": "Rename the branch?",
            "buttons": [
                {"id": "yes", "label": "Rename", "primary": True},
                {"id": "no", "label": "Keep"},
            ],
            "input": {"label": "New name", "value": "feat/x"},
        }
        reply, dialog = await self.answered_by_the_window(
            rig, ext, spec, {"button": "yes", "value": "feat/y"}
        )
        assert ok(reply) == {"button": "yes", "value": "feat/y"}
        assert dialog["source"] == ext and dialog["spec"]["title"] == "Rename the branch?"
        assert ok(await rig.ask(ext, "get", "ui.dialog")) is None
        [closed] = sink.values("ui.dialog_closed")
        assert closed["answer"] == {"button": "yes", "value": "feat/y"}
        assert sink.values("ui.dialog_opened")[0]["dialog"]["id"] == dialog["id"]

    async def test_dismissed_is_none(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        reply, _dialog = await self.answered_by_the_window(rig, ext, {"title": "Sure?"}, None)
        assert ok(reply) is None

    async def test_a_new_dialog_closes_the_open_one_unanswered(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        first = asyncio.create_task(rig.ask(ext, "call", "ui.show_dialog", spec={"title": "One"}))
        await rig.settle()
        second = asyncio.create_task(rig.ask(ext, "call", "ui.show_dialog", spec={"title": "Two"}))
        await rig.settle()
        assert ok(await asyncio.wait_for(first, 5)) is None
        assert ok(await rig.ask(ext, "get", "ui.dialog"))["spec"]["title"] == "Two"
        second.cancel()

    async def test_an_answer_to_a_dialog_no_longer_open_changes_nothing(self, rig: HostRig) -> None:
        await rig.window("w-1")
        opened = await rig.window_model("w-1", "open_dialog", value={"kind": "update"})
        dialog_id = opened["dialog"]["id"]
        assert (await rig.window_model("w-1", "answer_dialog", dialog_id=dialog_id))["answered"]
        again = await rig.window_model("w-1", "answer_dialog", dialog_id=dialog_id)
        assert again["answered"] is False

    async def test_a_spec_igni_cannot_draw_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        refused(await rig.ask(ext, "call", "ui.show_dialog", spec={"body": "no title"}), "invalid")
        refused(
            await rig.ask(ext, "call", "ui.show_dialog", spec={"title": "x", "html": "<b>"}),
            "invalid",
        )


class TestFocus:
    async def test_the_focused_window_is_the_one_that_said_so(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        sink = listen(rig, ext, "windows.focus_changed")
        await rig.window("w-1", connection="ws-a")
        await rig.window("w-2", connection="ws-b")
        assert ok(await rig.ask(ext, "get", "windows.focused")) is None
        await rig.window_writes("w-1", "focused", True)
        assert ok(await rig.ask(ext, "get", "windows.focused")) == "w-1"
        await rig.window_writes("w-2", "focused", True)
        assert ok(await rig.ask(ext, "get", "windows.focused")) == "w-2"
        listed = {w["id"]: w["focused"] for w in ok(await rig.ask(ext, "get", "windows.list"))}
        assert listed == {"w-1": False, "w-2": True}
        # The focused window is the active one: host.ui addresses it.
        ok(await rig.ask(ext, "call", "ui.open_panel", kind="search"))
        assert (await rig.window_pushes())[-1]["window"] == "w-2"
        await rig.window_writes("w-2", "focused", False)
        assert ok(await rig.ask(ext, "get", "windows.focused")) is None
        assert [(v["id"], v["focused"]) for v in sink.values("windows.focus_changed")] == [
            ("w-1", True),
            ("w-2", True),
            ("w-2", False),
        ]

    async def test_a_window_that_goes_away_loses_focus(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", connection="ws-a")
        await rig.window_writes("w-1", "focused", True)
        assert rig.model is not None
        rig.model.connection_closed("ws-a")
        assert ok(await rig.ask(ext, "get", "windows.focused")) is None


class TestTheComposer:
    async def test_the_draft_is_the_sessions_and_moves_with_it(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", session="boot")
        await rig.window_writes("w-1", "composer", {"text": "for boot", "mode": "chat"})
        assert rig.orchestrator is not None
        rig.orchestrator.client_state.set_value("w-1", "draft:other", "for other")
        await rig.window_writes("w-1", "session", "other")
        assert ok(await rig.ask(ext, "get", "ui.composer"))["text"] == "for other"
        # The window was told: the composer it shows changed with the session.
        last = (await rig.window_pushes("w-1"))[-1]
        assert last["field"] == "composer" and last["value"]["text"] == "for other"

    async def test_attachments_are_the_files_the_draft_refers_to(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", session="boot")
        await rig.window_writes(
            "w-1", "composer", {"text": "compare @src/a.py and @src/b.py:3-9 @code:p1"}
        )
        composer = ok(await rig.ask(ext, "get", "ui.composer"))
        assert composer["attachments"] == ["src/a.py", "src/b.py:3-9"]
        composer = ok(
            await rig.ask(
                ext,
                "call",
                "ui.set_composer",
                text="see @src/a.py",
                attachments=["src/a.py", "c.md"],
            )
        )
        assert composer["text"] == "see @src/a.py @c.md "
        assert composer["attachments"] == ["src/a.py", "c.md"]

    async def test_a_window_with_no_session_has_no_draft_to_write(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        refused(await rig.ask(ext, "call", "ui.set_composer", text="x"), "invalid")


def test_references_are_paths_not_code_pills() -> None:
    assert references("@a @b @a x@y @code:z\n@c") == ["a", "b", "c"]


class TestTheSessionAWindowShows:
    async def test_a_reload_writing_its_session_again_is_not_a_switch(self, rig: HostRig) -> None:
        # Every reload binds the window to its session again. One
        # ``session.switched`` per actual move, and an extension needs
        # no workspace grant to hear it — only ``sessions``.
        ext = rig.install(host=["sessions"])
        sink = listen(rig, ext, "session.switched")
        await rig.window("w-1")
        await rig.window_writes("w-1", "session", "boot")
        await rig.window("w-1")  # the reload
        await rig.window_writes("w-1", "session", "boot")
        await rig.window_writes("w-1", "session", "other")
        assert sink.values("session.switched") == [
            {"session_id": "boot", "window": "w-1"},
            {"session_id": "other", "window": "w-1"},
        ]

    async def test_an_extension_moves_the_window_and_the_window_hears_it(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["sessions"])
        await rig.window("w-1", session="boot")
        moved = ok(await rig.ask(ext, "set", "sessions.current", to="boot"))
        assert moved is None
        ok(await rig.ask(ext, "call", "sessions.open", session_id="s-2"))
        [push] = [p for p in await rig.window_pushes("w-1") if p["field"] == "session"]
        assert push["value"] == "s-2" and push["token"] == ""


async def test_a_full_stack_of_toasts_lets_the_oldest_go_and_says_so(rig: HostRig) -> None:
    ext = rig.install(host=["ui"])
    sink = listen(rig, ext, "ui.toast_dismissed")
    await rig.window("w-1")
    shown = [ok(await rig.ask(ext, "call", "ui.toast", text=f"#{n}")) for n in range(7)]
    on_screen = ok(await rig.ask(ext, "get", "ui.toasts"))
    assert [t["text"] for t in on_screen] == [f"#{n}" for n in range(1, 7)]
    assert [v["toast"]["id"] for v in sink.values("ui.toast_dismissed")] == [shown[0]["id"]]
