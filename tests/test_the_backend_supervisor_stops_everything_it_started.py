"""The backend supervisor takes down, in order, everything it brought up.

:class:`BackendSupervisor` is what makes a backend process end when it
should and leave nothing behind: a signal, the death of the window that
started it, or a closed transport all pull the same lever, and teardown
then cancels its background work, shuts the sessions down, closes the
transport and removes the discovery lockfile — in that order, and even
when one of those steps fails.

Only the pool and the transport are doubles here: they are the two
collaborators the supervisor is handed, and what it does to them is the
behaviour under test. Signals are real signals to this process, on a
real event loop.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import signal
import sys
from pathlib import Path

import pytest

from ember_code import __version__
from ember_code.backend import supervisor as supervisor_mod
from ember_code.backend.lockfile import Lockfile
from ember_code.backend.schemas_rpc import LifecyclePhase
from ember_code.backend.schemas_sessions import (
    EvictedRuntimeReport,
    EvictionReport,
    ShutdownReport,
)
from ember_code.backend.supervisor import BackendSupervisor

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


class _Transport:
    def __init__(self, *, fails: bool = False) -> None:
        self.closed = 0
        self._fails = fails

    async def close(self) -> None:
        self.closed += 1
        if self._fails:
            raise RuntimeError("socket already gone")


class _Pool:
    """The session pool as the supervisor sees it: shut down, evict idle."""

    def __init__(
        self,
        *,
        reports: list[ShutdownReport] | None = None,
        evictions: list[EvictionReport | Exception] | None = None,
    ) -> None:
        self.shutdowns = 0
        self.sweeps = 0
        self._reports = reports or []
        self._evictions = evictions or []

    async def shutdown(self) -> list[ShutdownReport]:
        self.shutdowns += 1
        return self._reports

    async def evict_idle(self) -> EvictionReport:
        self.sweeps += 1
        if not self._evictions:
            return EvictionReport()
        report = self._evictions.pop(0)
        if isinstance(report, Exception):
            raise report
        return report


class _Backend:
    def __init__(self, *, fails: bool = False) -> None:
        self.shutdowns = 0
        self._fails = fails

    async def shutdown(self) -> None:
        self.shutdowns += 1
        if self._fails:
            raise RuntimeError("half-built")


def _supervisor(tmp_path: Path, transport: _Transport | None = None) -> BackendSupervisor:
    return BackendSupervisor(
        transport=transport or _Transport(),
        loop=asyncio.get_running_loop(),
        project_dir=tmp_path,
    )


# ── Teardown ──────────────────────────────────────────────────────


async def test_teardown_shuts_the_pool_down_closes_the_transport_and_ends_stopped(tmp_path):
    transport = _Transport()
    pool = _Pool(reports=[ShutdownReport(ok=True, session_id="a")])
    sup = _supervisor(tmp_path, transport)
    sup.set_pool(pool)
    sup.mark_running()
    assert sup.phase is LifecyclePhase.RUNNING

    await sup.teardown()

    assert pool.shutdowns == 1
    assert transport.closed == 1
    assert sup.phase is LifecyclePhase.STOPPED


async def test_a_session_that_failed_to_shut_down_is_logged_not_swallowed(tmp_path, caplog):
    pool = _Pool(
        reports=[
            ShutdownReport(ok=False, session_id="s-1", error="db locked"),
            ShutdownReport(ok=False, session_id="", error="no id"),
        ]
    )
    sup = _supervisor(tmp_path)
    sup.set_pool(pool)

    with caplog.at_level(logging.WARNING, logger=supervisor_mod.__name__):
        await sup.teardown()

    messages = [r.getMessage() for r in caplog.records]
    assert "session s-1 shutdown failed: db locked" in messages
    assert "session <unknown> shutdown failed: no id" in messages


async def test_without_a_pool_teardown_shuts_down_the_backend_it_was_given(tmp_path):
    """Boot crashed before the pool existed: the fallback is still stopped."""
    backend = _Backend(fails=True)
    sup = _supervisor(tmp_path, _Transport(fails=True))
    sup.set_backend_fallback(backend)

    await sup.teardown()

    assert backend.shutdowns == 1
    assert sup.phase is LifecyclePhase.STOPPED


async def test_teardown_with_nothing_wired_still_stops(tmp_path):
    sup = _supervisor(tmp_path)

    await sup.teardown()

    assert sup.phase is LifecyclePhase.STOPPED


async def test_teardown_cancels_the_background_work_it_started(tmp_path, monkeypatch):
    monkeypatch.setenv("IGNI_PARENT_PID", str(os.getpid()))
    transport = _Transport()
    sup = _supervisor(tmp_path, transport)
    sup.set_pool(_Pool())
    sup.start_parent_watchdog()
    sup.start_transport_close_watcher()
    sup.start_evictor(sweep_interval_seconds=3600)
    tasks = [sup._parent_watch_task, sup._transport_close_task, sup._evictor_task]
    await asyncio.sleep(0)

    await sup.teardown()

    assert all(t is not None and t.done() for t in tasks)
    # Closed by teardown itself; the watcher was cancelled before it could.
    assert transport.closed == 1


# ── The discovery lockfile ────────────────────────────────────────


async def test_the_lockfile_is_published_and_removed_at_teardown(tmp_path):
    (tmp_path / ".igni").mkdir()
    sup = _supervisor(tmp_path)

    sup.write_discovery_lockfile(51234, ws_token="secret")

    payload = Lockfile(tmp_path).read()
    assert payload is not None
    assert payload.port == 51234
    assert payload.pid == os.getpid()
    assert payload.ws_token == "secret"
    assert payload.wire_version == BackendSupervisor._resolve_wire_version()

    await sup.teardown()

    assert Lockfile(tmp_path).read() is None


async def test_a_lockfile_that_cannot_be_written_is_a_warning_not_a_crash(tmp_path, caplog):
    blocked = tmp_path / "project"
    blocked.write_text("a file where the project directory should be")
    sup = _supervisor(blocked)

    with caplog.at_level(logging.WARNING, logger=supervisor_mod.__name__):
        sup.write_discovery_lockfile(1)

    assert any("could not write backend lockfile" in r.getMessage() for r in caplog.records)
    assert sup._lockfile is None


def test_the_wire_version_is_the_packages_version():
    assert BackendSupervisor._resolve_wire_version() == __version__


def test_an_unreadable_package_init_gives_an_unknown_version(monkeypatch):
    def _unreadable(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("gone")

    monkeypatch.setattr(Path, "read_text", _unreadable)

    assert BackendSupervisor._resolve_wire_version() == "0.0.0"


@pytest.mark.parametrize(("raw", "pid"), [("4242", 4242), ("", 0), ("not-a-pid", 0)])
def test_the_parent_pid_comes_from_the_environment(monkeypatch, raw, pid):
    monkeypatch.setenv("IGNI_PARENT_PID", raw)

    assert BackendSupervisor._resolve_parent_pid() == pid


# ── Signals ───────────────────────────────────────────────────────


@POSIX_ONLY
async def test_sigterm_asks_for_shutdown(tmp_path):
    sup = _supervisor(tmp_path)
    loop = asyncio.get_running_loop()
    sup.install_signal_handlers()
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(sup.wait_for_shutdown(), timeout=5)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
            loop.remove_signal_handler(sig)

    assert sup.shutdown_event.is_set()


@POSIX_ONLY
async def test_sigusr1_collects_garbage_and_sweeps_the_pool_at_once(tmp_path, monkeypatch):
    collected: list[bool] = []
    real_collect = gc.collect
    monkeypatch.setattr(supervisor_mod.gc, "collect", lambda: collected.append(True) or 0)
    pool = _Pool()
    sup = _supervisor(tmp_path)
    sup.set_pool(pool)
    loop = asyncio.get_running_loop()
    sup.install_signal_handlers()
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        for _ in range(100):
            if pool.sweeps:
                break
            await asyncio.sleep(0.01)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
            loop.remove_signal_handler(sig)
        real_collect()

    assert collected == [True]
    assert pool.sweeps == 1
    assert not sup.shutdown_event.is_set()


async def test_without_a_pool_sigusr1_only_collects(tmp_path):
    sup = _supervisor(tmp_path)

    sup._on_release_signal()  # no pool bound yet: nothing to sweep, nothing raised


async def test_shutdown_requests_are_idempotent(tmp_path):
    sup = _supervisor(tmp_path)

    sup.request_shutdown()
    sup.request_shutdown()

    await asyncio.wait_for(sup.wait_for_shutdown(), timeout=1)


# ── Background loops ──────────────────────────────────────────────


async def test_a_shutdown_closes_the_transport_so_receive_unblocks(tmp_path):
    transport = _Transport(fails=True)
    sup = _supervisor(tmp_path, transport)
    sup.start_transport_close_watcher()
    await asyncio.sleep(0)

    sup.request_shutdown()
    await asyncio.wait_for(sup._transport_close_task, timeout=5)

    assert transport.closed == 1


async def test_the_evictor_sweeps_until_shutdown_and_reports_failed_evictions(tmp_path, caplog):
    evicted = EvictionReport(
        evicted=[
            EvictedRuntimeReport(
                session_id="idle-1",
                idle_seconds=900,
                shutdown=ShutdownReport(ok=False, session_id="idle-1", error="stuck"),
            )
        ]
    )
    pool = _Pool(evictions=[evicted, RuntimeError("pool busy")])
    sup = _supervisor(tmp_path)
    sup.set_pool(pool)

    with caplog.at_level(logging.INFO, logger=supervisor_mod.__name__):
        sup.start_evictor(sweep_interval_seconds=0.01)
        for _ in range(200):
            if pool.sweeps >= 3:
                break
            await asyncio.sleep(0.01)
        sup.request_shutdown()
        await asyncio.wait_for(sup._evictor_task, timeout=5)

    messages = [r.getMessage() for r in caplog.records]
    assert "session pool: evicted 1 idle session(s): ['idle-1']" in messages
    assert "evicted session idle-1 shutdown failed: stuck" in messages
    assert "evictor sweep failed: pool busy" in messages


async def test_with_no_parent_named_the_watchdog_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("IGNI_PARENT_PID", "0")
    sup = _supervisor(tmp_path)

    await asyncio.wait_for(sup._watch_parent_loop(), timeout=1)

    assert not sup.shutdown_event.is_set()


async def test_a_dead_parent_shuts_the_backend_down_then_forces_the_exit(tmp_path, monkeypatch):
    """Graceful first; if that stalls, ``os._exit`` — never a zombie."""
    monkeypatch.setenv("IGNI_PARENT_PID", "4242")
    kills: list[tuple[int, int]] = []
    exits: list[int] = []
    monkeypatch.setattr(supervisor_mod.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(supervisor_mod.os, "_exit", exits.append)
    sleeps: list[float] = []

    async def _no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(supervisor_mod.asyncio, "sleep", _no_wait)
    sup = _supervisor(tmp_path)
    sup._parent_probe = type("Dead", (), {"is_alive": lambda self, pid: False})()

    await sup._watch_parent_loop()

    assert sup.shutdown_event.is_set()
    assert exits == [1]
    assert sleeps[0] == 5
    if sys.platform != "win32":
        assert kills == [(os.getpid(), signal.SIGTERM)]


async def test_a_live_parent_is_polled_until_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("IGNI_PARENT_PID", "4242")
    sup = _supervisor(tmp_path)
    polls: list[int] = []

    class _Alive:
        def is_alive(self, pid: int) -> bool:
            polls.append(pid)
            sup.request_shutdown()
            return True

    sup._parent_probe = _Alive()

    async def _no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(supervisor_mod.asyncio, "sleep", _no_wait)

    await sup._watch_parent_loop()

    assert polls == [4242]


def test_on_windows_the_receive_loop_is_not_nudged_with_a_signal(monkeypatch, tmp_path):
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(supervisor_mod.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(supervisor_mod.sys, "platform", "win32")
    loop = asyncio.new_event_loop()
    try:
        sup = BackendSupervisor(transport=_Transport(), loop=loop, project_dir=tmp_path)
        sup._nudge_receive_loop()
    finally:
        loop.close()

    assert kills == []
