"""The parent-PID watchdog has to work on the platform that needs it most.

The BE self-terminates when the FE that spawned it dies. Two mechanisms do
that: the FE's process_manager killing the BE's process group, and this
watchdog. On Windows only the watchdog exists — ``add_signal_handler`` raises
``NotImplementedError`` there and there is no process group to kill — and the
watchdog probed liveness with ``os.kill(pid, 0)``.

That is not a liveness probe on Windows. CPython maps signal 0 onto
``CTRL_C_EVENT`` (same integer) and takes the console branch, so the call is
``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` — it never raises
``ProcessLookupError``, which was the only exception the loop read, and when
the pid names a console process group it delivers a Ctrl-C to the FE the
watchdog was only asking about.

These tests pin both halves: the probe never signals anything on Windows, and
the loop asks the probe rather than reaching for ``os.kill`` itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess

import pytest

from ember_code.backend import supervisor as supervisor_mod
from ember_code.backend.supervisor import BackendSupervisor, ParentProcessProbe


class _NullTransport:
    async def close(self) -> None:
        return None


class _DeadParent:
    """Probe stub: the parent is gone."""

    def is_alive(self, pid: int) -> bool:
        return False


class _LiveParent:
    def is_alive(self, pid: int) -> bool:
        return True


@pytest.fixture
def kill_spy(monkeypatch):
    """Record every ``os.kill`` the code under test attempts.

    Also stops a POSIX test host from actually SIGTERM-ing the pytest
    process when the watchdog nudges its own receive loop.
    """
    calls: list[tuple] = []
    monkeypatch.setattr(supervisor_mod.os, "kill", lambda *args: calls.append(args))
    return calls


class TestTheProbe:
    def test_the_windows_branch_never_signals_the_process_it_asks_about(self, kill_spy):
        """``os.kill(pid, 0)`` on Windows is a Ctrl-C, not a question."""
        probe = ParentProcessProbe(windows=True)

        probe.is_alive(4242)

        assert kill_spy == [], (
            "the Windows branch called os.kill — on Windows signal 0 is "
            "CTRL_C_EVENT, so this is the FE being interrupted by its own watchdog"
        )

    def test_the_posix_branch_sees_a_live_process(self):
        assert ParentProcessProbe(windows=False).is_alive(os.getpid()) is True

    def test_the_posix_branch_sees_a_dead_one(self):
        # ``wait`` reaps, so the pid is fully gone by the time we probe
        # it — no zombie to mistake for a live parent.
        proc = subprocess.Popen(["true"])
        pid = proc.pid
        proc.wait()

        assert ParentProcessProbe(windows=False).is_alive(pid) is False

    def test_a_zero_or_negative_pid_is_never_alive(self):
        assert ParentProcessProbe(windows=False).is_alive(0) is False
        assert ParentProcessProbe(windows=True).is_alive(-1) is False


class TestTheWatchdogLoop:
    async def test_it_shuts_the_backend_down_when_the_probe_says_the_parent_died(
        self, tmp_path, kill_spy
    ):
        sup = BackendSupervisor(
            transport=_NullTransport(),
            loop=asyncio.get_running_loop(),
            project_dir=tmp_path,
        )
        sup._parent_pid = 4242
        sup._parent_probe = _DeadParent()

        task = asyncio.create_task(sup._watch_parent_loop())
        try:
            await asyncio.wait_for(sup.shutdown_event.wait(), timeout=2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert sup.shutdown_event.is_set()
        # The only signal sent is the POSIX nudge to ourselves. Nothing
        # is ever sent to the parent.
        assert all(call[0] == os.getpid() for call in kill_spy), (
            f"the watchdog signalled something other than itself: {kill_spy}"
        )

    async def test_it_stays_quiet_while_the_parent_lives(self, tmp_path, kill_spy):
        sup = BackendSupervisor(
            transport=_NullTransport(),
            loop=asyncio.get_running_loop(),
            project_dir=tmp_path,
        )
        sup._parent_pid = 4242
        sup._parent_probe = _LiveParent()

        task = asyncio.create_task(sup._watch_parent_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert not sup.shutdown_event.is_set()
        assert kill_spy == []


class TestTheReceiveLoopNudge:
    def test_posix_sigterms_itself(self, tmp_path, kill_spy):
        assert supervisor_mod.sys.platform != "win32", "this case asserts the POSIX nudge"
        sup = BackendSupervisor(
            transport=_NullTransport(),
            loop=None,  # type: ignore[arg-type]
            project_dir=tmp_path,
        )

        sup._nudge_receive_loop()

        assert kill_spy == [(os.getpid(), signal.SIGTERM)]

    def test_windows_does_not_terminate_itself(self, tmp_path, kill_spy, monkeypatch):
        """``os.kill(self, SIGTERM)`` on Windows is ``TerminateProcess``.

        There is no handler to run, so the graceful path — pool shutdown,
        transport close, removing the discovery lockfile — would be skipped
        and the next boot would find a stale ``.igni/backend.lock``.
        """
        monkeypatch.setattr(supervisor_mod.sys, "platform", "win32")
        sup = BackendSupervisor(
            transport=_NullTransport(),
            loop=None,  # type: ignore[arg-type]
            project_dir=tmp_path,
        )

        sup._nudge_receive_loop()

        assert kill_spy == []
