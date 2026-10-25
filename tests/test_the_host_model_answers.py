"""The host object model, answering against a running backend.

``docs/HOST_OBJECT_MODEL.md``: an extension reaches every part of igni
as objects with properties, signals and methods, granted per object by
``permissions.host``. These drive :class:`HostModel` the way its two
channels do, over a backend assembled as the app assembles it
(:class:`tests.host_model_helpers.HostRig`) — the real orchestrator,
the real RPC tables, the real transport tap — with stand-in sessions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.host.spec import PropertySpec
from ember_code.extensions.registry import ExtensionState
from ember_code.protocol import messages as msg
from tests.host_model_helpers import HostRig, Sink

EVERYTHING = [p.name for p in HOST_SCHEMA.permissions if p.name != "workspace"]


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


class TestEveryPropertyAnswersInItsType:
    async def test_each_built_property_reads_back_what_the_schema_promises(
        self, rig: HostRig
    ) -> None:
        """Every stage-1 property, read through the model, fits its schema type.

        The rig runs the model in ``strict`` mode, which fails the
        request when an answer does not fit — so a member whose
        translation drifts from igni's wire is caught here, not by an
        extension.
        """
        ext = rig.install(host=EVERYTHING, workspace="read")
        (rig.project / "README.md").write_text("hello")
        await rig.window()
        failures: list[str] = []
        for spec in HOST_SCHEMA.objects:
            for prop in spec.properties:
                if prop.stage != 1:
                    continue
                path = f"{spec.path}.{prop.name}" if spec.path else prop.name
                reply = await rig.ask(ext, "get", path)
                if not reply.ok:
                    failures.append(f"{path}: {reply.error}")
        assert not failures, failures

    async def test_every_property_is_one_of_ours(self) -> None:
        assert all(isinstance(p, PropertySpec) for o in HOST_SCHEMA.objects for p in o.properties)


class TestGrants:
    async def test_an_object_not_granted_does_not_exist(self, rig: HostRig) -> None:
        ext = rig.install(host=["sessions"])
        message = refused(await rig.ask(ext, "get", "chat.processing"), "not_granted")
        assert "chat.read" in message and "permissions.host" in message
        ok(await rig.ask(ext, "get", "sessions.current"))

    async def test_a_prefix_grant_reaches_every_object_under_it(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.*"])
        ok(await rig.ask(ext, "get", "chat.processing"))
        ok(await rig.ask(ext, "get", "chat.approvals.waiting"))
        refused(await rig.ask(ext, "get", "sessions.list"), "not_granted")

    async def test_its_own_state_and_settings_need_no_grant(self, rig: HostRig) -> None:
        ext = rig.install()
        ok(await rig.ask(ext, "call", "app.state.set", key="cursor", value="42"))
        assert ok(await rig.ask(ext, "call", "app.state.get", key="cursor")) == "42"
        assert ok(await rig.ask(ext, "get", "self.id")) == ext
        assert ok(await rig.ask(ext, "get", "self.state"))["state"] == "READY"

    async def test_one_extension_never_sees_anothers_state(self, rig: HostRig) -> None:
        mine = rig.install("acme.one")
        theirs = rig.install("acme.two")
        ok(await rig.ask(mine, "call", "app.state.set", key="k", value="mine"))
        assert ok(await rig.ask(theirs, "call", "app.state.get", key="k")) is None

    async def test_workspace_is_granted_by_the_workspace_permission_and_its_paths(
        self, rig: HostRig
    ) -> None:
        none = rig.install("acme.none")
        docs = rig.install("acme.docs", workspace="read", workspacePaths=["docs/**"])
        (rig.project / "docs").mkdir()
        (rig.project / "docs" / "a.md").write_text("a")
        (rig.project / "secret.txt").write_text("s")
        refused(await rig.ask(none, "call", "workspace.read", path="docs/a.md"), "not_granted")
        assert (
            ok(await rig.ask(docs, "call", "workspace.read", path="docs/a.md"))["contents"] == "a"
        )
        refused(await rig.ask(docs, "call", "workspace.read", path="secret.txt"), "not_granted")
        refused(await rig.ask(docs, "call", "workspace.read", path="../outside"), "refused")

    async def test_an_uninstalled_extension_is_nobody(self, rig: HostRig) -> None:
        refused(await rig.ask("acme.ghost", "get", "self.id"), "not_found")


class TestTheSchemaIsTheAuthority:
    async def test_an_unknown_member_is_not_found(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        refused(await rig.ask(ext, "get", "chat.nothing"), "not_found")

    async def test_a_member_defined_and_not_built_says_so(self, rig: HostRig) -> None:
        # No member is stage 2 today; the mechanism is held to its
        # promise against a schema where one is.
        assert rig.model is not None
        app = HOST_SCHEMA.object("app")
        assert app is not None
        unbuilt = tuple(
            p.model_copy(update={"stage": 2}) if p.name == "version" else p for p in app.properties
        )
        rig.model.schema = HOST_SCHEMA.model_copy(
            update={
                "objects": tuple(
                    o.model_copy(update={"properties": unbuilt}) if o.path == "app" else o
                    for o in HOST_SCHEMA.objects
                )
            }
        )
        ext = rig.install(host=["app"])
        message = refused(await rig.ask(ext, "get", "app.version"), "stage_2")
        assert "not yet available" in message
        assert not HOST_SCHEMA.stage_two()

    async def test_a_property_that_is_not_writable_cannot_be_set(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.read"])
        refused(await rig.ask(ext, "set", "chat.processing", to=True), "read_only")

    async def test_parameters_are_checked_against_the_signature(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.post"])
        refused(await rig.ask(ext, "call", "chat.post"), "invalid")
        refused(await rig.ask(ext, "call", "chat.post", text="x", louder=True), "invalid")
        refused(await rig.ask(ext, "call", "chat.post", text=["not", "text"]), "invalid")


class TestChatPost:
    async def test_a_message_appears_as_the_extensions_and_the_agent_answers(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["chat.post", "chat.read"])
        sink = Sink()
        assert rig.model is not None
        subscriber = rig.model.bus.attach(ext, sink)
        rig.model.bus.subscribe(subscriber, ["chat.message_added", "chat.run_completed"])

        ok(await rig.ask(ext, "call", "chat.post", text="Two issues in auth.py."))
        await rig.settle()

        [echo] = rig.windows.of(msg.UserMessageReceived)
        assert echo.text == "Two issues in auth.py."
        assert echo.extension_id == ext
        assert echo.client_id == ""  # nobody's echo: every window paints it
        # The agent was asked, and told who was asking.
        [(agent_text, _)] = rig.session.ran
        assert f'<extension-message from="{ext}">' in agent_text
        assert "Two issues in auth.py." in agent_text
        assert "not typed by the person" in agent_text
        # The run streamed to the windows as any run does.
        assert rig.windows.of(msg.RunStarted) and rig.windows.of(msg.RunCompleted)
        [added] = sink.values("chat.message_added")
        assert added["from_extension"] == ext
        assert sink.values("chat.run_completed")

    async def test_queue_waits_for_the_next_turn_labelled_too(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.post"])
        ok(await rig.ask(ext, "call", "chat.queue", text="after this"))
        await rig.settle()
        [echo] = rig.windows.of(msg.UserMessageReceived)
        assert echo.queued and echo.extension_id == ext
        assert rig.session.runtime.queue[-1].endswith("after this\n</extension-message>")
        assert rig.session.ran == []

    async def test_posting_needs_its_own_grant(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.read"])
        refused(await rig.ask(ext, "call", "chat.post", text="hi"), "not_granted")
        assert rig.session.ran == []


class TestViews:
    SPEC = {"root": "c", "elements": {"c": {"type": "Card", "props": {"title": "Reviewer"}}}}

    async def test_append_shows_a_block_keeps_it_and_never_tells_the_model(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["chat.append"])
        view_id = ok(await rig.ask(ext, "call", "chat.append", view=self.SPEC, title="Review"))
        await rig.settle()

        [push] = rig.windows.pushes("orchestrate_event")
        assert push.payload["type"] == "visualization_delta"
        assert push.payload["spec_id"] == view_id
        assert push.payload["final"] is True
        assert json.loads(push.payload["json"]) == self.SPEC
        # Kept in the session's log, so a reload shows it again.
        assert [e.payload["spec_id"] for e in rig.session.events] == [view_id]
        # Never a turn: the model was not asked anything.
        assert rig.session.ran == []
        [listed] = ok(await rig.ask(ext, "get", "chat.views.list"))
        assert listed["view_id"] == view_id and listed["source"] == ext

    async def test_replace_updates_the_same_block(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.append"])
        view_id = ok(await rig.ask(ext, "call", "chat.append", view=self.SPEC))
        changed = {"root": "c", "elements": {"c": {"type": "Card", "props": {"title": "Done"}}}}
        ok(await rig.ask(ext, "call", "chat.replace", view_id=view_id, view=changed))
        await rig.settle()
        ids = [p.payload["spec_id"] for p in rig.windows.pushes("orchestrate_event")]
        assert ids == [view_id, view_id]
        [listed] = ok(await rig.ask(ext, "get", "chat.views.list"))
        assert listed["spec"] == changed

    async def test_one_extension_cannot_replace_anothers_block(self, rig: HostRig) -> None:
        mine = rig.install("acme.one", host=["chat.append"])
        theirs = rig.install("acme.two", host=["chat.append"])
        view_id = ok(await rig.ask(mine, "call", "chat.append", view=self.SPEC))
        refused(
            await rig.ask(theirs, "call", "chat.replace", view_id=view_id, view=self.SPEC),
            "refused",
        )

    async def test_a_view_must_be_a_json_render_spec(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.append"])
        refused(await rig.ask(ext, "call", "chat.append", view={"html": "<b>no</b>"}), "invalid")


class TestWritesAreRefusedWhereThePersonsWouldBe:
    async def test_switching_to_another_projects_session_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=["sessions"])
        await rig.window()
        assert rig.orchestrator is not None
        elsewhere = rig.tmp / "elsewhere"
        elsewhere.mkdir()
        rig.orchestrator._dir_registry.set_dir("other", elsewhere)  # type: ignore[union-attr]
        message = refused(await rig.ask(ext, "set", "sessions.current", to="other"), "refused")
        assert "different project" in message

    async def test_switching_model_to_one_not_configured_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=["models"])
        refused(await rig.ask(ext, "set", "models.current", to="imaginary"), "refused")
        ok(await rig.ask(ext, "set", "models.current", to="large"))
        assert rig.session.settings.models.default == "large"

    async def test_answering_an_approval_nobody_asked_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.approvals"])
        refused(
            await rig.ask(
                ext, "call", "chat.approvals.answer", requirement_id="r1", action="confirm"
            ),
            "not_found",
        )


class TestApprovals:
    async def test_an_extension_sees_and_answers_what_the_agent_asks(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.approvals"])
        assert rig.model is not None
        sink = Sink()
        rig.model.bus.subscribe(rig.model.bus.attach(ext, sink), ["chat.approvals.requested"])
        await rig.transport.send(
            msg.RunPaused(
                session_id="boot",
                run_id="r",
                requirements=[
                    msg.HITLRequest(requirement_id="q1", tool_name="shell", friendly_name="Run")
                ],
            )
        )
        [waiting] = ok(await rig.ask(ext, "get", "chat.approvals.waiting"))
        assert waiting["requirement_id"] == "q1"
        assert sink.values("chat.approvals.requested")[0]["tool_name"] == "shell"

        ok(
            await rig.ask(
                ext,
                "call",
                "chat.approvals.answer",
                requirement_id="q1",
                action="confirm",
                choice="once",
            )
        )
        await rig.settle()
        assert rig.session.answers == [("q1", "confirm", "once")]
        # Every window dismisses the dialog, as when the person answers.
        assert [m.requirement_id for m in rig.windows.of(msg.RequirementResolved)] == ["q1"]
        assert ok(await rig.ask(ext, "get", "chat.approvals.waiting")) == []


class TestCommands:
    async def test_a_command_answers_the_extension_not_the_windows(self, rig: HostRig) -> None:
        ext = rig.install(host=["commands"])
        outcome = ok(await rig.ask(ext, "call", "commands.run", command="/ctx"))
        assert outcome["content"] == "ran /ctx"
        assert rig.session.commands == ["/ctx"]
        assert not rig.windows.of(msg.CommandResult)

    async def test_set_output_style_runs_the_command_igni_has(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        display = ok(await rig.ask(ext, "call", "app.set_output_style", name="terse"))
        assert display["output_style"] == "terse"
        assert rig.session.commands == ["/output-style terse"]


class TestWindowsAndUi:
    async def test_a_window_is_seen_opening_and_closing(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        assert rig.model is not None
        sink = Sink()
        rig.model.bus.subscribe(
            rig.model.bus.attach(ext, sink), ["windows.opened", "windows.closed"]
        )
        await rig.window("w-1", connection="ws-a")
        [window] = ok(await rig.ask(ext, "get", "windows.list"))
        assert window["id"] == "w-1" and window["kind"] == "tauri"
        rig.model.connection_closed("ws-a")
        assert ok(await rig.ask(ext, "get", "windows.list")) == []
        assert [v["id"] for v in sink.values("windows.opened")] == ["w-1"]
        assert [v["id"] for v in sink.values("windows.closed")] == ["w-1"]

    async def test_navigate_writes_the_windows_state_and_tells_it(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        pages = ok(await rig.ask(ext, "call", "ui.navigate", page="plugins"))
        assert pages == [{"kind": "plugins", "label": "plugins", "params": {}}]
        await rig.settle()
        [push] = await rig.window_pushes("w-1")
        assert push["field"] == "route" and push["value"][0]["kind"] == "plugins"
        assert push["token"] == ""  # nobody's echo: the window applies it
        assert ok(await rig.ask(ext, "call", "ui.back")) == []

    async def test_what_the_window_does_is_a_signal(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui", "sessions"])
        assert rig.model is not None
        sink = Sink()
        rig.model.bus.subscribe(
            rig.model.bus.attach(ext, sink), ["ui.sidebar_changed", "session.switched", "changed"]
        )
        await rig.window("w-1")
        await rig.window_writes("w-1", "sidebar", {"open": True})
        await rig.window_writes("w-1", "session", "boot")
        # The same value again — a reload re-saving it — is not a change.
        await rig.window_writes("w-1", "sidebar", {"open": True})
        [sidebar] = sink.values("ui.sidebar_changed")
        assert sidebar == {
            "window": "w-1",
            "sidebar": {"open": True, "width": None, "section": None},
        }
        assert sink.values("session.switched") == [{"session_id": "boot", "window": "w-1"}]
        changed = {c["path"] for c in sink.values("changed")}
        assert {"ui.sidebar", "sessions.current"} <= changed

    async def test_a_page_reaches_its_own_window(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", connection="ws-a")
        await rig.window("w-2", connection="ws-b")
        await rig.window_writes("w-2", "sidebar", {"open": True})  # w-2 is the active window
        await rig.window_writes("w-1", "sidebar", {"open": False})
        await rig.window_writes("w-2", "sidebar", {"open": False})
        reply = await rig.ask(ext, "call", "ui.set_sidebar", open=True, home_window="w-1")
        assert ok(reply)["open"] is True
        await rig.settle()
        assert (await rig.window_pushes())[-1]["window"] == "w-1"

    async def test_any_window_by_id(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui", "sessions"])
        await rig.window("w-1", connection="ws-a")
        await rig.window("w-2", connection="ws-b")
        ok(await rig.ask(ext, "call", "ui.set_sidebar", open=True, window="w-1"))
        await rig.settle()
        assert (await rig.window_pushes())[-1]["window"] == "w-1"
        refused(await rig.ask(ext, "get", "ui.route", window="w-9"), "not_found")
        refused(await rig.ask(ext, "get", "sessions.current", window="w-1"), "invalid")


class TestOpenExternal:
    """``host.app.open_external``: a link out of igni, opened by a window."""

    async def test_the_window_asked_about_is_told_to_open_it(self, rig: HostRig) -> None:
        ext = rig.install(host=["links"])
        await rig.window("w-1", connection="ws-a")
        await rig.window("w-2", connection="ws-b")
        url = "https://code.example.com/repos/42?tab=commits#top"
        assert (
            ok(await rig.ask(ext, "call", "app.open_external", url=url, home_window="w-1")) is None
        )
        await rig.settle()
        [push] = [p.payload for p in rig.windows.pushes("open_external")]
        assert push == {"window": "w-1", "url": url, "source": ext}

    @pytest.mark.parametrize(
        "url",
        [
            "http://code.example.com/",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "vscode://acme.reviewer/open",
            "https://user:secret@code.example.com/",
            "https:///no-host",
            "https://code.example.com/a b",
            "/relative/path",
            "https://code.example.com/" + "a" * 2048,
        ],
    )
    async def test_only_an_https_link_opens(self, rig: HostRig, url: str) -> None:
        ext = rig.install(host=["links"])
        await rig.window("w-1")
        refused(await rig.ask(ext, "call", "app.open_external", url=url), "invalid")
        await rig.settle()
        assert rig.windows.pushes("open_external") == []

    async def test_it_needs_the_links_grant(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        await rig.window("w-1")
        refused(
            await rig.ask(ext, "call", "app.open_external", url="https://example.com/"),
            "not_granted",
        )

    async def test_with_no_window_there_is_nowhere_to_open_it(self, rig: HostRig) -> None:
        ext = rig.install(host=["links"])
        refused(
            await rig.ask(ext, "call", "app.open_external", url="https://example.com/"),
            "not_found",
        )


class TestSignals:
    async def test_signals_carry_the_model_shape_not_igni_wire(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.read"], workspace="read", workspacePaths=["src/**"])
        assert rig.model is not None
        sink = Sink()
        rig.model.bus.subscribe(rig.model.bus.attach(ext, sink), ["chat.run_completed"])
        await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1"))
        await rig.transport.send(
            msg.PushNotification(
                session_id="boot",
                channel="file_edited",
                payload={"path": str(rig.project / "src/a.py")},
            )
        )
        await rig.transport.send(
            msg.PushNotification(
                session_id="boot",
                channel="file_edited",
                payload={"path": str(rig.project / "notes.md")},
            )
        )
        await rig.transport.send(msg.RunCompleted(session_id="boot", run_id="r1", duration=2.0))
        [run] = sink.values("chat.run_completed")
        # The files the run changed — those the workspace grant reaches.
        assert run["changed_files"] == ["src/a.py"]
        assert run["session_id"] == "boot" and run["duration"] == 2.0

    async def test_an_ungranted_subscription_is_accepted_and_never_delivered(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=[])
        assert rig.model is not None
        sink = Sink()
        outcome = rig.model.bus.subscribe(
            rig.model.bus.attach(ext, sink), ["chat.run_started", "chat.nope"]
        )
        assert outcome == {
            "subscribed": [],
            "not_granted": ["chat.run_started"],
            "unknown": ["chat.nope"],
        }
        await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1"))
        assert sink.got == []

    async def test_an_extension_hears_its_own_health_and_all_of_it_with_the_grant(
        self, rig: HostRig
    ) -> None:
        mine = rig.install("acme.one")
        watcher = rig.install("acme.two", host=["extensions"])
        assert rig.model is not None
        own, all_ = Sink(), Sink()
        rig.model.bus.subscribe(
            rig.model.bus.attach(mine, own), ["self.state_changed", "extension.state-changed"]
        )
        rig.model.bus.subscribe(
            rig.model.bus.attach(watcher, all_), ["extension.state-changed", "self.state_changed"]
        )
        rig.registry.set_state(mine, ExtensionState.FAILED, "its service died")
        assert own.values("self.state_changed") == [
            {"extension_id": mine, "state": "FAILED", "reason": "its service died"}
        ]
        assert own.values("extension.state-changed") == []  # not granted `extensions`
        assert [v["extension_id"] for v in all_.values("extension.state-changed")] == [mine]
        assert all_.values("self.state_changed") == []  # about acme.one, not itself

    async def test_a_window_rpc_write_is_a_signal(self, rig: HostRig) -> None:
        ext = rig.install(host=["models"])
        assert rig.model is not None
        sink = Sink()
        rig.model.bus.subscribe(rig.model.bus.attach(ext, sink), ["models.changed"])
        assert rig.orchestrator is not None
        await rig.orchestrator.dispatch(
            msg.RPCRequest(id="fe-1", method="switch_model", args={"model_name": "large"})
        )
        assert sink.values("models.changed") == [{"current": "large"}]


class TestExtensionsAndTheirWebHome:
    async def test_web_url_is_the_service_a_dev_install_names(self, rig: HostRig) -> None:
        ext = rig.install(host=["extensions"])
        entry = rig.registry.get(ext)
        assert entry is not None
        assert ok(await rig.ask(ext, "get", "self.web_url")) is None
        rig.store.save_settings(entry.manifest, {"serviceUrl": "https://reviewer.acme.test"})
        assert ok(await rig.ask(ext, "get", "self.web_url")) == "https://reviewer.acme.test"
        [info] = ok(await rig.ask(ext, "get", "extensions.installed"))
        assert info["web_url"] == "https://reviewer.acme.test"

    async def test_its_own_settings_round_trip_and_signal(self, rig: HostRig) -> None:
        ext = rig.install()
        assert rig.model is not None
        sink = Sink()
        rig.model.bus.subscribe(rig.model.bus.attach(ext, sink), ["self.settings_changed"])
        ok(await rig.ask(ext, "set", "self.settings", to={"serviceUrl": "https://x.test"}))
        assert ok(await rig.ask(ext, "get", "self.settings"))["serviceUrl"] == "https://x.test"
        assert sink.values("self.settings_changed") == [
            {"values": {"serviceUrl": "https://x.test"}}
        ]


class TestAPageReachesTheSameModel:
    """A page's ``bridge.host…`` arrives as the ``host_model`` RPC."""

    async def page(self, rig: HostRig, caller: str | None, **args: Any) -> Any:
        assert rig.orchestrator is not None
        await rig.orchestrator.dispatch(
            msg.RPCRequest(id="page-1", method="host_model", caller=caller, args=args)
        )
        [reply] = [m for m in rig.windows.of(msg.RPCResponse) if m.id == "page-1"]
        rig.windows.sent.remove(reply)
        return reply

    async def test_a_request_is_answered_as_the_frames_extension(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.read"])
        reply = await self.page(rig, ext, id="h1", op="get", path="chat.processing")
        assert reply.result == {
            "op": "reply",
            "id": "h1",
            "ok": True,
            "result": False,
            "error": None,
        }
        reply = await self.page(rig, ext, id="h2", op="get", path="sessions.list")
        assert reply.result["ok"] is False
        assert reply.result["error"]["code"] == "not_granted"

    async def test_only_an_extensions_page_reaches_it(self, rig: HostRig) -> None:
        reply = await self.page(rig, None, id="h1", op="get", path="chat.processing")
        assert reply.error and "only an extension's page" in reply.error

    async def test_a_pages_signals_reach_its_frames_through_the_windows(self, rig: HostRig) -> None:
        ext = rig.install(host=["chat.read"])
        reply = await self.page(
            rig, ext, id="h1", op="subscribe", path="", signals=["chat.run_started"]
        )
        assert reply.result["result"]["subscribed"] == ["chat.run_started"]
        await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1", agent_name="main"))
        await rig.settle()
        [push] = rig.windows.pushes("host_signal")
        assert push.payload["extension_id"] == ext
        assert push.payload["signal"] == "chat.run_started"
        assert push.payload["value"]["run_id"] == "r1"

    async def test_a_page_is_in_its_own_window(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", connection="ws-a")
        await rig.window("w-2", connection="ws-b")
        reply = await self.page(
            rig,
            ext,
            id="h1",
            op="call",
            path="ui.navigate",
            params={"page": "mcp"},
            home_window="w-1",
        )
        assert reply.result["ok"]
        await rig.settle()
        assert (await rig.window_pushes())[-1]["window"] == "w-1"
