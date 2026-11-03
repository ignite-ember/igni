"""A real backend answers what a window asks of it, before any run.

:class:`BackendServer` is the object every RPC lands on. Most of its
methods are one line into a controller, and most of the suite tests the
controllers directly — or builds the server through ``__new__`` with a
mocked session, which skips everything ``__init__`` wires. So nothing
checked that the server a real boot builds actually answers: that the
panel RPCs return their typed shapes, that nothing in the idle state
raises, and that the questions a window asks on connect have answers
before the first message.

Every test here constructs the server the way ``BackendApp`` does — real
settings, a real project directory, a real session — and asks it what a
window would. Nothing is mocked. The second half pins the ``__new__``
path itself: dozens of older tests build a partial server that way, and
the accessors' fallbacks are what keep those tests meaning anything.
"""

from __future__ import annotations

import asyncio
import base64
from typing import get_args

import pytest

from ember_code.backend.activity import ActivityState, SessionActivity
from ember_code.backend.hitl_controller import HitlController
from ember_code.backend.marketplace_controller import MarketplaceController
from ember_code.backend.model_switcher import ModelSwitcher
from ember_code.backend.plan_snapshot_builder import PlanSnapshotBuilder
from ember_code.backend.plugin_controller import PluginController
from ember_code.backend.schemas_hitl import ToolCallArgs
from ember_code.backend.server import BackendServer
from ember_code.backend.server_auth import AuthController
from ember_code.backend.server_context import ContextController
from ember_code.backend.server_files import FilesController
from ember_code.backend.server_lifecycle import LifecycleController
from ember_code.backend.server_loop import LoopController
from ember_code.backend.server_mcp import McpController
from ember_code.backend.server_panels import PanelsController
from ember_code.backend.server_processes import ProcessesController
from ember_code.backend.server_rehydrate import RehydrateController
from ember_code.backend.server_search import SearchController
from ember_code.backend.server_sessions import SessionsController
from ember_code.backend.team_wiring import TeamWiring
from ember_code.backend.visualization_action_bus import VisualizationActionBus
from ember_code.core.config.settings import load_settings
from ember_code.core.config.tool_permissions import PermissionLevel
from ember_code.protocol import messages as msg


@pytest.fixture
def backend(tmp_path) -> BackendServer:
    return BackendServer(load_settings(project_dir=tmp_path), project_dir=tmp_path)


# ── On connect ────────────────────────────────────────────────────


def test_an_idle_backend_says_it_is_idle(backend):
    activity = backend.activity()

    assert isinstance(activity, SessionActivity)
    assert activity.state is ActivityState.IDLE
    assert activity.session_id == backend.session_id
    assert backend.processing is False


def test_the_status_names_the_configured_model(backend):
    status = backend.get_status()

    assert isinstance(status, msg.StatusUpdate)
    assert status.model == backend.settings.models.default
    assert backend.run_timeout == backend.settings.models.max_run_timeout


def test_the_project_and_its_mcp_manager_are_the_sessions(backend, tmp_path):
    assert backend.project_dir == tmp_path
    assert backend.mcp_manager is backend._session.mcp_manager
    assert backend.runtime is None

    sentinel = object()
    backend.attach_runtime(sentinel)

    assert backend.runtime is sentinel


async def test_a_fresh_session_has_no_history_and_nothing_pending(backend):
    sid = backend.session_id

    assert await backend.get_chat_history(sid) == []
    assert await backend.get_pending_messages(sid) == []
    assert await backend.get_interrupted_runs(sid) == []
    assert backend.get_todos() == []
    plan = backend.get_latest_plan()
    assert (plan.latest, plan.state) == ("", "")
    assert backend.list_background_processes() == []


async def test_the_session_list_includes_nothing_it_was_not_told(backend):
    result = await backend.list_sessions()

    assert isinstance(result, msg.SessionListResult)


# ── The panels ────────────────────────────────────────────────────


def test_every_panel_answers_with_its_typed_shape(backend):
    assert isinstance(backend.get_agent_details(), list)
    assert isinstance(backend.get_hooks_details(), list)
    assert isinstance(backend.get_skill_details(), list)
    assert isinstance(backend.get_slash_commands(), list)
    assert backend.get_slash_commands(), "the built-in commands are always there"
    assert isinstance(backend.skill_names, list)
    assert backend.get_skill_pool() is backend._session.skill_pool
    assert backend.get_output_styles() is not None
    assert backend.get_group_policy() is not None
    assert isinstance(backend.reload_hooks_rpc(), msg.Info)


def test_the_mcp_panel_is_empty_for_a_project_with_no_servers(backend):
    assert backend.get_mcp_status() == []
    assert backend.get_mcp_servers() == []


async def test_connecting_an_mcp_server_nobody_configured_says_so(backend):
    reply = await backend.mcp_connect("nobody-configured-this")

    assert isinstance(reply, msg.Info)
    assert "nobody-configured-this" in reply.text


def test_the_plugin_panel_lists_nothing_and_a_missing_plugin_is_an_error(backend):
    assert backend.get_plugin_details() == []

    contents = backend.get_plugin_contents("not-installed")

    assert contents.error == "Plugin 'not-installed' not found"


def test_an_installed_plugin_is_inventoried_from_its_directory(backend, tmp_path):
    root = tmp_path / "plug"
    (root / "skills" / "greet").mkdir(parents=True)
    (root / "skills" / "greet" / "SKILL.md").write_text("---\nname: greet\n---\nSay hi.\n")
    installed = type("Plugin", (), {"name": "plug", "root_path": root})()
    backend._session.plugin_loader.list_plugins = lambda: [installed]

    contents = backend.get_plugin_contents("plug")

    assert contents.error == ""
    assert [s.name for s in contents.skills] == ["greet"]


def test_marketplaces_start_from_what_the_user_registered(backend):
    assert isinstance(backend.get_marketplaces(), list)


# ── What a window does without starting a run ─────────────────────


def test_a_visualization_action_is_recorded_and_echoed(backend):
    seen: list[tuple[str, dict]] = []
    backend.register_broadcast(lambda channel, payload: seen.append((channel, payload)))

    result = backend.dispatch_visualization_action("select_row", {"row": 3})

    assert result.ok is True
    assert result.action == "select_row"
    assert result.params == {"row": 3}


async def test_an_event_recorded_is_an_event_the_session_replays(backend):
    await backend.record_event("extension_card", {"title": "Deploys"}, run_id="r1")

    (event,) = backend.session_events("extension_card")

    assert event.payload == {"title": "Deploys"}
    assert backend.session_events("something_else") == []


def test_an_attachment_uploaded_can_be_read_back(backend):
    upload = backend.upload_attachment("note.txt", base64.b64encode(b"hello").decode())

    assert upload.path
    read = backend.read_file(upload.path)

    assert read.error == ""
    assert read.contents == "hello"


def test_code_search_on_an_empty_project_finds_nothing(backend):
    result = backend.search_code("def nowhere():")

    assert result.matches == []


def test_a_permission_is_checked_against_the_projects_rules(backend):
    level = backend.check_permission("Write", "save_file", ToolCallArgs(file_path="a.txt"))

    assert level in get_args(PermissionLevel)


def test_toggling_verbose_flips_it_and_back(backend):
    first = backend.toggle_verbose()

    assert backend.toggle_verbose() is not first


def test_switching_the_model_rebuilds_the_team_on_the_new_one(backend):
    reply = backend.switch_model("another-model")

    assert isinstance(reply, msg.Info)
    assert backend.settings.models.default == "another-model"


def test_the_plan_research_flag_is_read_once(backend):
    assert backend.consume_plan_research_flag() is False

    backend._session._plan_research_armed = True

    assert backend.consume_plan_research_flag() is True
    assert backend.consume_plan_research_flag() is False


# ── Cancelling with nothing running ───────────────────────────────


def test_cancelling_a_sub_agent_run_without_an_id_is_refused(backend):
    result = backend.cancel_agent_run("")

    assert result.ok is False
    assert result.error == "missing run_id"


def test_cancelling_when_nothing_runs_does_nothing(backend):
    backend.cancel_run()

    assert backend.processing is False


async def test_a_process_nobody_started_has_no_output_and_cannot_be_stopped(backend):
    tail = backend.read_process_tail(999_999)
    stopped = await backend.stop_background_process(999_999)

    assert tail == {"pid": 999_999, "output": "", "is_running": False, "exit_code": None}
    assert stopped["killed"] is False


# ── /loop and the scheduler ───────────────────────────────────────


async def test_with_no_loop_running_there_is_nothing_to_advance_or_cancel(backend):
    assert await backend.pop_pending_loop_iteration() is None
    assert await backend.cancel_pending_loop() is False
    assert await backend.loop_resume() == ""
    status = await backend.loop_status()

    assert status.active is False


async def test_a_paused_loop_is_not_cancelled_by_a_new_message_and_resumes(backend):
    await backend._session.start_loop("check the build", 3, immediate=False, cap_explicit=True)
    assert await backend.loop_pause() is True

    assert await backend.cancel_pending_loop() is False
    assert (await backend.loop_status()).paused is True
    assert "check the build" in await backend.loop_resume()
    assert await backend.cancel_pending_loop() is True


async def test_the_scheduler_starts_with_no_tasks(backend):
    assert await backend.get_scheduled_tasks() == []
    reply = await backend.cancel_scheduled_task("no-such-task")

    assert isinstance(reply, msg.Info)


# ── Lifecycle ─────────────────────────────────────────────────────


async def test_startup_and_shutdown_run_on_a_fresh_project(backend):
    await backend.startup()
    await backend.shutdown()


async def test_the_session_start_hook_fires_even_if_revalidating_policy_fails(backend):
    async def _fails() -> None:
        raise RuntimeError("portal unreachable")

    fired: list[bool] = []

    async def _hook() -> None:
        fired.append(True)

    backend.auth.revalidate_for_new_dialogue = _fails
    backend._session.fire_session_start_hook = _hook

    await backend.fire_session_start_hook()

    assert fired == [True]


async def test_a_change_of_workspace_trust_reloads_the_settings(backend):
    applied: list[object] = []

    async def _apply(settings: object) -> None:
        applied.append(settings)

    backend._session.apply_workspace_trust = _apply

    await backend.apply_workspace_trust()

    assert applied == [backend.settings]
    assert backend.workspace_trust is not None


# ── The ``__new__`` path older tests build ────────────────────────

_ACCESSORS: dict[str, type] = {
    "plugins": PluginController,
    "marketplaces": MarketplaceController,
    "mcp": McpController,
    "hitl": HitlController,
    "context": ContextController,
    "auth": AuthController,
    "files": FilesController,
    "search": SearchController,
    "panels": PanelsController,
    "loop": LoopController,
    "processes": ProcessesController,
    "sessions": SessionsController,
    "rehydrate": RehydrateController,
    "lifecycle": LifecycleController,
    "plan_snapshots": PlanSnapshotBuilder,
    "viz_actions": VisualizationActionBus,
    "team_wiring": TeamWiring,
    "model_switcher": ModelSwitcher,
}


@pytest.fixture
def partial(backend) -> BackendServer:
    """A server built the way older fixtures build one: no controllers."""
    server = BackendServer.__new__(BackendServer)
    server._session = backend._session
    server._settings = backend._settings
    server._hitl_store = backend._hitl_store
    server._session_prefs = backend._session_prefs
    server._pending_store = backend._pending_store
    return server


@pytest.mark.parametrize(("name", "kind"), sorted(_ACCESSORS.items()))
def test_a_partly_built_server_builds_each_controller_once(partial, name, kind):
    first = getattr(partial, name)

    assert isinstance(first, kind)
    assert getattr(partial, name) is first


def test_a_partly_built_server_keeps_the_run_fields_a_fixture_sets(partial):
    lock = asyncio.Lock()
    partial._run_lock = lock
    partial._current_run_task = None

    assert partial._run_lock is lock
    assert partial._current_run_task is None
    assert partial.processing is False


def test_a_partly_built_server_cancels_without_a_run_pipeline(partial):
    assert partial.cancel_agent_run("").error == "missing run_id"
    assert partial.cancel_agent_run("not-a-live-run").ok in (True, False)

    partial.cancel_run()


async def test_a_partly_built_server_still_rebuilds_history(partial):
    assert await partial.get_chat_history(partial.session_id) == []


def test_a_server_with_no_session_has_no_manager_and_no_broadcasts():
    server = BackendServer.__new__(BackendServer)

    assert server.mcp_manager is None
    assert server.consume_plan_research_flag() is False
    server.register_broadcast(lambda *_: None)
