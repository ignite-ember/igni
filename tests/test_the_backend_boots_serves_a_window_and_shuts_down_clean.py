"""The backend boots, serves a window, and leaves nothing behind when told to stop.

:class:`BackendApp` is the composition root a desktop shell, an IDE plugin
and the cloud all start: build the transport, load settings with the
group's policy, build the session, wire some thirty collaborators, print
the ready line, serve, and tear it all down in a ``finally``. Its own
tests used to stop at ``_start_extensions``; the process itself was only
ever run as a subprocess by the browser suite, which no Python coverage
sees and which says nothing more specific than "the page loaded".

Here the real ``run()`` goes end to end in this process, on a loopback
WebSocket: the ready line names the port a window connects on, the
discovery lockfile appears beside the project, a real WebSocket client
connects and is answered, and a ``shutdown`` from that window stops the
backend — lockfile gone, every background task stopped. No collaborator
is replaced. The egress guard the whole suite runs under is what holds
that a boot with nobody signed in reaches nothing outside the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from ember_code.backend import app as app_mod
from ember_code.backend.app import BackendApp, TransportBuilder
from ember_code.backend.lockfile import Lockfile
from ember_code.backend.schemas_rpc import LifecyclePhase
from ember_code.core.config.group_policy import GroupPolicyGate, GroupPolicyUnavailable
from ember_code.protocol import messages as msg
from ember_code.transport.websocket import CompositeTransport, WebSocketServerTransport

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the loop's signal handlers")


@contextlib.contextmanager
def _signal_handlers_restored():
    """``run()`` installs SIGTERM/SIGINT/SIGUSR1 on the running loop — the test's."""
    loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGUSR1", None)):
            if sig is not None:
                loop.remove_signal_handler(sig)


async def _until(predicate, timeout: float = 30) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition never held")
        await asyncio.sleep(0.02)


def _ws_port(app: BackendApp) -> int | None:
    ws = app._transport_builder.ws_transport
    if ws is None or app._orchestrator is None or app._supervisor is None:
        return None
    if app._supervisor.phase is not LifecyclePhase.RUNNING:
        return None
    return ws.port


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".igni").mkdir()
    monkeypatch.delenv("IGNI_PARENT_PID", raising=False)
    return tmp_path


@POSIX_ONLY
async def test_a_window_connects_is_served_and_its_shutdown_stops_the_backend(project, capsys):
    app = BackendApp(
        socket_path=None,
        ws_port=0,
        project_dir=project,
        resume_session_id=None,
        additional_dirs=None,
    )
    with _signal_handlers_restored():
        running = asyncio.create_task(app.run())
        try:
            await _until(lambda: _ws_port(app) is not None or running.done())
            assert not running.done(), running.exception()
            port = _ws_port(app)

            ready = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            assert ready == {
                "status": "ready",
                "ws_port": port,
                "ws_url": f"ws://127.0.0.1:{port}",
            }
            lock = Lockfile(project).read()
            assert lock is not None and lock.port == port

            async with connect(f"ws://127.0.0.1:{port}") as window:
                welcome = json.loads(await asyncio.wait_for(window.recv(), 10))
                assert welcome["type"]
                await window.send(msg.Shutdown().model_dump_json())
                await asyncio.wait_for(running, 30)
        finally:
            if not running.done():
                app._supervisor.request_shutdown()
                await asyncio.wait_for(running, 30)

    assert app._supervisor.phase is LifecyclePhase.STOPPED
    assert Lockfile(project).read() is None


@POSIX_ONLY
async def test_a_signal_stops_a_backend_nobody_ever_connected_to(project, capsys):
    """A shell that quits before its webview ever connected.

    The backend waited for its first window with no timeout, and a shutdown
    only closes the transport — which does not wake that wait. So the
    process kept running for nobody, teardown never ran, and the discovery
    lockfile stayed behind. This hung until the fix in
    ``BackendApp._wait_for_a_window``.
    """
    app = BackendApp(
        socket_path=None,
        ws_port=0,
        project_dir=project,
        resume_session_id=None,
        additional_dirs=None,
    )
    with _signal_handlers_restored():
        running = asyncio.create_task(app.run())
        await _until(lambda: _ws_port(app) is not None or running.done())
        assert not running.done(), running.exception()

        app._supervisor.request_shutdown()
        await asyncio.wait_for(running, 30)

    assert app._supervisor.phase is LifecyclePhase.STOPPED
    assert Lockfile(project).read() is None


# ── The transport the flags ask for ───────────────────────────────


async def test_the_websocket_flag_alone_builds_a_websocket(tmp_path):
    builder = TransportBuilder(socket_path=None, ws_port=0, ws_token="t0k")
    transport = await builder.build()
    try:
        assert isinstance(transport, WebSocketServerTransport)
        assert builder.ws_transport is transport
        # The lockfile carries a program's token, never the launch secret.
        assert builder.program_token and builder.program_token != "t0k"
        assert transport.port > 0
    finally:
        await transport.close()


@POSIX_ONLY
async def test_both_flags_mirror_one_session_to_both_transports(tmp_path):
    socket_path = Path("/tmp") / f"igni-test-{id(tmp_path)}.sock"
    builder = TransportBuilder(socket_path=str(socket_path), ws_port=0)
    transport = await builder.build()
    try:
        assert isinstance(transport, CompositeTransport)
        assert builder.ws_transport is not None
    finally:
        await transport.close()
        socket_path.unlink(missing_ok=True)


# ── Settings with the group's policy ──────────────────────────────


def test_a_required_policy_that_cannot_be_had_closes_the_gate_but_boots(project, monkeypatch):
    """The person can still sign in — which is how a pack is fetched."""
    local = app_mod.load_settings(project_dir=project)

    def _unavailable(*args, **kwargs):
        if kwargs.get("group_policy_fetcher") is not None:
            raise GroupPolicyUnavailable("no verified pack")
        return local

    monkeypatch.setattr(app_mod, "load_settings", _unavailable)
    app = BackendApp(
        socket_path=None,
        ws_port=0,
        project_dir=project,
        resume_session_id=None,
        additional_dirs=None,
    )
    try:
        settings = app._load_settings_with_group_policy()

        assert settings is local
        assert GroupPolicyGate.reason() == "no verified pack"
    finally:
        GroupPolicyGate.reset()
