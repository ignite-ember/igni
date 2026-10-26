"""igni's side of the producer VM's guest contract (``docs/CLOUD.md`` §9).

Three files on ``/run/igni`` the guest agent reads or writes
(``igni__cloudvm``'s ``docs/guest-protocol.md``): ``activity.json``
(igni's ``app.activity``), ``ready`` (igni listens), and
``producer.env`` (who the VM is for, written after igni started from a
warm base). And what a snapshot restore leaves behind: pooled
connections to drop, a notification stream to reopen.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from ember_code.backend.activity import Activity, ActivityState, SessionActivity
from ember_code.backend.app import BackendApp
from ember_code.backend.guest import (
    ActivityFile,
    AtomicFile,
    CloudGuest,
    GuestPaths,
    ProducerBinding,
    ProducerEnv,
    ReadyFile,
    ResumeWatch,
    TokenTenant,
    UnboundRefusal,
    WarmImports,
)
from ember_code.core.auth.credentials import EnvironmentCredentials
from ember_code.core.config.schemas.storage_and_paths import StorageConfig
from ember_code.core.config.settings import load_settings
from ember_code.core.db.engine import cache
from ember_code.core.db.engine_registry import EngineRegistry
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.scope import Tenant, tenancy
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.profile import PROFILE_ENV
from ember_code.core.session.pending_messages import PendingMessageStore
from ember_code.extensions.host.broadcasts import BroadcastSignals
from ember_code.extensions.host.model import HostState
from ember_code.extensions.principal import SignedInPerson
from ember_code.extensions.store import ExtensionStore
from ember_code.notifications.stream import NotificationStreamFollower
from ember_code.protocol import messages as msg
from ember_code.transport.websocket import WebSocketServerTransport

ORG = "org-acme"
PRODUCER = "person:u-ada"


def _jwt(**claims: Any) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


ADA_TOKEN = _jwt(sub="u-ada", org=ORG, exp=int(time.time()) + 3600)


@pytest.fixture
def cloud(monkeypatch) -> None:
    """The cloud profile, with nothing named yet: a warm base's igni."""
    monkeypatch.setenv(PROFILE_ENV, "cloud")
    for name in (
        StorageConfig.DATABASE_URL_ENV,
        EnvironmentCredentials.TOKEN_FILE_ENV,
        EnvironmentCredentials.TOKEN_ENV,
        ProducerEnv.ORG_ENV,
        ProducerEnv.PRODUCER_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch) -> Path:
    run = tmp_path / "run-igni"
    run.mkdir()
    monkeypatch.setenv(GuestPaths.RUN_DIR_ENV, str(run))
    return run


def _hand_over(run: Path, database_url: str, *, org: str = ORG, producer: str = PRODUCER) -> None:
    """What the node agent does after a restore: the token, then producer.env."""
    AtomicFile(run / "token").write(ADA_TOKEN.encode())
    AtomicFile(run / "producer.env").write(
        (
            f"IGNI_ACCESS_TOKEN_FILE={run / 'token'}\n"
            f"IGNI_PRODUCER={producer}\n"
            f"IGNI_ORG={org}\n"
            f"IGNI_DATABASE_URL={database_url}\n"
        ).encode()
    )


# ── Paths ─────────────────────────────────────────────────────────


def test_the_files_are_on_run_igni_unless_moved():
    paths = GuestPaths.from_env({})
    assert paths.activity == Path("/run/igni/activity.json")
    assert paths.ready == Path("/run/igni/ready")
    assert paths.producer_env == Path("/run/igni/producer.env")
    moved = GuestPaths.from_env({GuestPaths.RUN_DIR_ENV: "/x", GuestPaths.READY_ENV: "/y/ok"})
    assert moved.activity == Path("/x/activity.json")
    assert moved.ready == Path("/y/ok")


def test_the_desktop_has_no_guest_contract(monkeypatch):
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    assert CloudGuest.current() is None


def test_an_atomic_write_leaves_only_the_file(tmp_path: Path):
    target = tmp_path / "activity.json"
    AtomicFile(target).write(b"one")
    AtomicFile(target).write(b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["activity.json"]


def test_ready_is_written_and_cleared(tmp_path: Path):
    ready = ReadyFile(tmp_path / "ready")
    ready.mark()
    assert ready.path.read_text().strip() == str(os.getpid())
    ready.clear()
    ready.clear()
    assert not ready.path.exists()


def test_what_a_bound_backend_imports_is_imported_before_ready(tmp_path: Path):
    class Recorded(WarmImports):
        def run(self) -> None:
            self.ready_then = (tmp_path / "ready").exists()

    warm = Recorded()
    guest = CloudGuest(GuestPaths.from_env({GuestPaths.RUN_DIR_ENV: str(tmp_path)}), warm=warm)
    guest.listening()
    assert warm.ready_then is False, "imports first: the snapshot is taken at ready"
    assert (tmp_path / "ready").exists()


def test_every_warm_module_is_importable_here():
    warm = WarmImports()
    warm.run()
    assert warm.skipped == [], "a renamed module would silently stop being warmed"


def test_a_missing_optional_module_is_skipped():
    warm = WarmImports(("json", "igni_no_such_module"))
    warm.run()
    assert warm.skipped == ["igni_no_such_module"]


# ── activity.json ─────────────────────────────────────────────────


class Activities:
    """An ``app.activity`` the test sets, and a clock it moves."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.clients = 0
        self.sessions: list[SessionActivity] = []

    def activity(self) -> Activity:
        return Activity.of(self.sessions)

    def running(self) -> None:
        self.sessions = [SessionActivity(session_id="s1", state=ActivityState.RUNNING)]

    def waiting(self) -> None:
        self.sessions = [
            SessionActivity(session_id="s1", state=ActivityState.WAITING, waiting_approvals=2)
        ]

    def file(self, path: Path) -> ActivityFile:
        return ActivityFile(
            path, activity=self.activity, clients=lambda: self.clients, clock=lambda: self.now
        )


def test_the_activity_file_is_app_activity_and_who_is_connected(tmp_path: Path):
    world = Activities()
    world.waiting()
    world.clients = 2
    activity = world.file(tmp_path / "activity.json")
    assert activity.refresh()
    written = json.loads(activity.path.read_text())
    assert written["state"] == "waiting"
    assert written["suspendable"] is True
    assert written["waiting_approvals"] == 2
    assert written["running"] == 0
    assert written["clients"] == 2
    assert written["sessions"][0]["session_id"] == "s1"
    assert written["written_at_ms"] == 1_000_000


def test_it_is_written_when_it_changes_and_not_otherwise(tmp_path: Path):
    world = Activities()
    activity = world.file(tmp_path / "activity.json")
    assert activity.refresh()
    world.now += 30
    assert not activity.refresh(), "idle, unchanged: nothing to say"
    world.running()
    assert activity.refresh()
    assert json.loads(activity.path.read_text())["suspendable"] is False
    world.clients = 1
    assert activity.refresh(), "a window connecting is a change"


def test_a_running_igni_rewrites_it_before_the_guest_agent_calls_it_stale(tmp_path: Path):
    world = Activities()
    world.running()
    activity = world.file(tmp_path / "activity.json")
    assert activity.refresh()
    world.now += 1
    assert not activity.refresh()
    world.now += ActivityFile.HEARTBEAT_SECONDS
    assert activity.refresh()
    assert ActivityFile.HEARTBEAT_SECONDS <= 5, "the protocol asks for every 5 s at most"


async def test_it_follows_activity_changed_and_is_removed_when_igni_stops(tmp_path: Path):
    world = Activities()
    activity = world.file(tmp_path / "activity.json")
    activity.start()
    assert json.loads(activity.path.read_text())["state"] == "idle"
    world.running()
    activity.changed({"state": "running"})
    assert json.loads(activity.path.read_text())["state"] == "running"
    await activity.stop()
    assert not activity.path.exists()


def test_activity_changed_reaches_a_listener():
    class Backend:
        value: dict[str, Any] = {"state": "idle"}

        def activity(self) -> dict[str, Any]:
            return self.value

    class Model:
        def __init__(self) -> None:
            self.state = HostState()
            self.backend = Backend()
            self.emitted: list[str] = []

        def emit(self, wire: str, value: Any = None, **_: Any) -> list[str]:
            self.emitted.append(wire)
            return []

    model = Model()
    signals = BroadcastSignals(model)  # type: ignore[arg-type]
    heard: list[dict[str, Any]] = []
    signals.on_activity(heard.append)
    signals.activity_moved()
    signals.activity_moved()
    model.backend.value = {"state": "running"}
    signals.activity_moved()
    assert heard == [{"state": "idle"}, {"state": "running"}]
    assert model.emitted == ["app.activity_changed", "app.activity_changed"]


# ── producer.env ──────────────────────────────────────────────────


def test_producer_env_takes_igni_variables_only():
    env = ProducerEnv.parse(
        "# from the node agent\n"
        "IGNI_DATABASE_URL='postgresql://r:p@db/igni'\n"
        "IGNI_ORG = acme\n"
        "PATH=/evil\n"
        "\n"
        "garbage\n"
    )
    assert env.values == {"IGNI_DATABASE_URL": "postgresql://r:p@db/igni", "IGNI_ORG": "acme"}
    assert env.database_url == "postgresql://r:p@db/igni"


def test_binding_is_needed_until_something_names_a_database(tmp_path: Path):
    environ: dict[str, str] = {}
    binding = ProducerBinding(tmp_path / "producer.env", environ=environ)
    assert binding.needed(), "a warm base: nothing named"
    environ[StorageConfig.DATABASE_URL_ENV] = "sqlite:///x.db"
    assert not binding.needed(), "started with its producer, as before"
    (tmp_path / "producer.env").write_text("IGNI_DATABASE_URL=sqlite:///y.db\n")
    assert binding.needed(), "the file is the source once it is there"


def test_it_says_what_it_is_waiting_for(cloud, tmp_path: Path):
    path = tmp_path / "producer.env"
    binding = ProducerBinding(path, environ={})
    assert "to be written" in binding.attempt().waiting_for
    path.write_text(f"IGNI_ORG={ORG}\n")
    assert "IGNI_DATABASE_URL" in binding.attempt().waiting_for
    path.write_text(f"IGNI_DATABASE_URL=sqlite:///x.db\nIGNI_ACCESS_TOKEN_FILE={tmp_path / 't'}\n")
    assert "token" in binding.attempt().waiting_for
    (tmp_path / "t").write_text(ADA_TOKEN)
    assert binding.attempt().bound


@pytest.mark.parametrize(
    ("org", "producer", "complaint"),
    [("org-other", PRODUCER, "IGNI_ORG"), (ORG, "person:u-eve", "IGNI_PRODUCER")],
)
def test_a_producer_env_that_disagrees_with_the_token_does_not_bind(
    cloud, tmp_path: Path, org: str, producer: str, complaint: str
):
    _hand_over(tmp_path, "sqlite:///x.db", org=org, producer=producer)
    environ: dict[str, str] = {}
    attempt = ProducerBinding(tmp_path / "producer.env", environ=environ).attempt()
    assert not attempt.bound
    assert complaint in attempt.waiting_for
    assert environ == {}, "nothing is applied until it all holds"


def test_the_token_names_the_tenant(cloud, tmp_path: Path, monkeypatch):
    (tmp_path / "token").write_text(ADA_TOKEN)
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(tmp_path / "token"))
    assert TokenTenant()() == Tenant(org=ORG, producer=PRODUCER)


def test_requests_before_binding_are_refused():
    rpc = UnboundRefusal.answer(msg.RPCRequest(id="r1", method="get_status", session_id="s"))
    assert isinstance(rpc, msg.RPCResponse)
    assert (rpc.id, rpc.session_id, rpc.result) == ("r1", "s", None)
    assert "not bound" in (rpc.error or "")
    other = UnboundRefusal.answer(msg.UserMessage(id="m1", text="hi"))
    assert isinstance(other, msg.Error) and other.id == "m1"


async def test_a_changed_producer_is_reported_after_binding(cloud, tmp_path: Path, monkeypatch):
    for name in ("IGNI_ACCESS_TOKEN_FILE", "IGNI_ORG", "IGNI_PRODUCER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(StorageConfig.DATABASE_URL_ENV, raising=False)
    _hand_over(tmp_path, "sqlite:///x.db")
    binding = ProducerBinding(tmp_path / "producer.env", poll_seconds=0.01)
    await asyncio.wait_for(binding.bind(), timeout=5)
    changed: list[ProducerEnv] = []
    binding.watch(changed.append)
    _hand_over(tmp_path, "sqlite:///x.db")  # rewritten as it was: nothing to say
    await asyncio.sleep(0.05)
    assert changed == []
    _hand_over(tmp_path, "sqlite:///other.db")
    for _ in range(100):
        if changed:
            break
        await asyncio.sleep(0.01)
    await binding.stop()
    assert [c.database_url for c in changed] == ["sqlite:///other.db"]


# ── Late binding, end to end ──────────────────────────────────────


async def test_an_igni_started_before_its_producer_binds_when_producer_env_appears(
    cloud, run_dir: Path, database_url: DatabaseUrl, tmp_path: Path, monkeypatch
):
    """A warm base's igni: listening, refusing, then bound to the database it is handed."""
    guest = CloudGuest(
        GuestPaths.from_env(),
        binding=ProducerBinding(GuestPaths.from_env().producer_env, poll_seconds=0.01),
    )
    guest.starting()
    # Engines other tests opened on the run's database go, so that "no
    # engine on it" below means this igni opened none.
    cache.dispose_all()
    transport = WebSocketServerTransport(port=0)
    await transport.start()
    try:
        guest.listening()
        assert (run_dir / "ready").exists(), "the warm base may be snapshotted now"
        binding = asyncio.create_task(guest.bind(transport))

        async with connect(f"ws://127.0.0.1:{transport.port}") as window:
            assert json.loads(await window.recv())["type"] == "welcome"
            await window.send(msg.RPCRequest(id="r1", method="get_status").model_dump_json())
            answer = json.loads(await asyncio.wait_for(window.recv(), timeout=5))
            assert answer["type"] == "rpc_response" and answer["id"] == "r1"
            assert "not bound" in answer["error"]
            assert not binding.done()
            assert database_url.url not in cache.registry._sync_engines, "no database yet"

            _hand_over(run_dir, database_url.url)
            bound = await asyncio.wait_for(binding, timeout=5)
            assert bound is not None and bound.database_url == database_url.url

            # The backend's boot from here on, as ``BackendApp.run`` does it.
            assert os.environ[StorageConfig.DATABASE_URL_ENV] == database_url.url
            settings = load_settings(project_dir=tmp_path)
            assert settings.storage.database_url == database_url.url
            tenancy.use(SignedInPerson(settings.auth.credentials_file, ExtensionStore()).tenant)
            database = StorageLocator.from_settings(settings).project_database(tmp_path)
            assert (database.scope.org, database.scope.producer) == (ORG, PRODUCER)
            store = PendingMessageStore(database)
            store.record_received("s1", "after the restore")
            assert [m.text for m in store.list_pending("s1")] == ["after the restore"]

            # Requests now reach whoever serves the transport, not the refusal.
            await window.send(msg.RPCRequest(id="r2", method="get_status").model_dump_json())
            async for message in transport.receive():
                assert message.id == "r2"
                break
    finally:
        await guest.stop()
        await transport.close()
    assert not (run_dir / "ready").exists()


class Booted(Exception):
    """Where the test stops the backend: at its first producer-specific step."""


async def test_the_backend_loads_nothing_of_a_producer_before_it_is_bound(
    cloud, run_dir: Path, tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ProducerBinding, "POLL_SECONDS", 0.01)
    (run_dir / "ready").write_text("left by an igni that exited\n")
    seen: dict[str, Any] = {}

    def load_settings_now(_self: BackendApp) -> Any:
        seen["database_url"] = os.environ.get(StorageConfig.DATABASE_URL_ENV)
        seen["cwd"] = Path(os.getcwd()).resolve()
        raise Booted

    monkeypatch.setattr(BackendApp, "_load_settings_with_group_policy", load_settings_now)
    app = BackendApp(
        socket_path=None,
        ws_port=0,
        project_dir=project,
        resume_session_id=None,
        additional_dirs=None,
    )
    assert isinstance(app._guest, CloudGuest)
    run = asyncio.create_task(app.run())
    try:
        for _ in range(500):
            if app._transport is not None:
                break
            await asyncio.sleep(0.01)
        assert (run_dir / "ready").read_text().strip() == str(os.getpid())
        await asyncio.sleep(0.05)
        assert not run.done() and seen == {}, "waiting for a producer"
        _hand_over(run_dir, f"sqlite:///{tmp_path / 'shared.db'}")
        with pytest.raises(Booted):
            await asyncio.wait_for(run, timeout=5)
    finally:
        if app._transport is not None:
            await app._transport.close()
    assert seen == {"database_url": f"sqlite:///{tmp_path / 'shared.db'}", "cwd": project.resolve()}
    assert not (run_dir / "ready").exists(), "an igni that stopped is not ready"


# ── After a restore ───────────────────────────────────────────────


class Clocks:
    def __init__(self) -> None:
        self.wall = 1_000.0
        self.monotonic = 50.0


async def test_a_wall_clock_jump_is_a_restore():
    clocks = Clocks()
    watch = ResumeWatch(wall=lambda: clocks.wall, monotonic=lambda: clocks.monotonic)
    told: list[float] = []

    async def heard(away: float) -> None:
        told.append(away)

    watch.on_resume(heard)
    clocks.wall += 1
    clocks.monotonic += 1
    assert not await watch.check(), "both clocks moved together"
    clocks.wall += 2
    clocks.monotonic += 0.5
    assert not await watch.check(), "a slow loop, not a restore"
    clocks.wall += 3601  # post_restore stepped the wall clock
    clocks.monotonic += 1
    assert await watch.check()
    assert told == [3600.0]


async def test_a_failing_step_does_not_stop_the_others():
    clocks = Clocks()
    watch = ResumeWatch(wall=lambda: clocks.wall, monotonic=lambda: clocks.monotonic)
    ran: list[str] = []

    async def broken(_away: float) -> None:
        raise RuntimeError("boom")

    async def fine(_away: float) -> None:
        ran.append("fine")

    watch.on_resume(broken)
    watch.on_resume(fine)
    clocks.wall += 60
    assert await watch.check()
    assert ran == ["fine"]


async def test_pools_are_emptied_and_the_engines_kept(database_url: DatabaseUrl):
    registry = EngineRegistry()
    engine = registry.get_engine(database_url)
    async_engine = registry.get_async_engine(database_url)
    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")
    async with async_engine.connect() as connection:
        await connection.exec_driver_sql("SELECT 1")
    pooled = engine.pool
    await registry.reset_pools()
    assert registry.get_engine(database_url) is engine
    assert registry.get_async_engine(database_url) is async_engine
    assert engine.pool is not pooled, "the old connections went with the old pool"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1
    async with async_engine.connect() as connection:
        assert (await connection.exec_driver_sql("SELECT 1")).scalar() == 1
    await registry.dispose()


async def test_the_notification_stream_reconnects_keeping_its_cursor():
    opened: list[str | None] = []

    class Follower(NotificationStreamFollower):
        async def _follow_once(self):  # type: ignore[override]
            opened.append(self.cursor)
            await asyncio.sleep(3600)

    follower = Follower(None, on_reconnect=lambda: None, on_wake=lambda: None)  # type: ignore[arg-type]
    follower._cursor = "evt-41"
    await follower.reconnect()
    assert not follower.following, "a stopped follower stays stopped"
    follower.start()
    await asyncio.sleep(0)
    await follower.reconnect()
    await asyncio.sleep(0)
    assert opened == ["evt-41", "evt-41"]
    await follower.stop()


async def test_serving_keeps_the_file_and_reconnects_after_a_restore(run_dir: Path, monkeypatch):
    clocks = Clocks()
    guest = CloudGuest(
        GuestPaths.from_env(),
        resume=ResumeWatch(wall=lambda: clocks.wall, monotonic=lambda: clocks.monotonic),
    )
    calls: list[str] = []

    async def reset_pools() -> None:
        calls.append("pools")

    monkeypatch.setattr(cache, "reset_pools", reset_pools)

    class Notifications:
        async def resumed(self, _away: float) -> None:
            calls.append("notifications")

    class Model:
        def __init__(self) -> None:
            self.backend = self
            self.broadcasts = self
            self.listeners: list[Any] = []

        def activity(self) -> dict[str, Any]:
            return Activity.of([]).model_dump(mode="json")

        def on_activity(self, listener: Any) -> None:
            self.listeners.append(listener)

    class Transport:
        inner = type("Inner", (), {"client_count": 3})()

    model = Model()
    guest.serving(model=model, transport=Transport(), notifications=Notifications())
    try:
        written = json.loads((run_dir / "activity.json").read_text())
        assert (written["state"], written["suspendable"], written["clients"]) == ("idle", True, 3)
        assert model.listeners == [guest.activity.changed]  # type: ignore[union-attr]
        clocks.wall += 600
        assert await guest.resume.check()
        assert calls == ["pools", "notifications"]
    finally:
        await guest.stop()
    assert not (run_dir / "activity.json").exists()
