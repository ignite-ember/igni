"""The shell process layer raised AttributeError on every Windows kill.

``os.killpg`` and ``os.getpgid`` are POSIX-only *names*. On Windows they
are not present on ``os`` at all, so the call raises ``AttributeError``
while its arguments are being evaluated — the same shape as the
``signal.SIGUSR1`` start-up crash fixed in e8ce739, and the same reason
the guards that were there did not help:

* ``ManagedProcess.kill`` wrapped the call in
  ``contextlib.suppress(ProcessLookupError, OSError)``. That names
  neither ``AttributeError`` nor anything it inherits from, and a
  ``with`` block cannot catch an exception raised while building the
  call inside it anyway. The exception left ``kill`` before the
  ``self.proc.kill()`` line below it ever ran — so nothing died.
  ``ESC`` stopped cancelling the foreground command, ``stop_process``
  stopped stopping, and ``ProcessRegistry.kill_all`` aborted mid-loop at
  BE shutdown with the pid map left uncleared.
* ``ProcessRegistry._persist_add`` wrapped it in
  ``try/except (ProcessLookupError, OSError)``. Same miss, and this one
  is on the *spawn* path: it is called from ``announce_start``, so
  ``background=True`` — the path the shell tool's docstring says you
  MUST use for anything long-running — failed outright.

These tests simulate the platform by deleting the attributes rather than
by skipping on POSIX, matching
``tests/test_the_backend_starts_on_windows.py``: a test that only runs
on Windows is a test that never runs here.
"""

from __future__ import annotations

import contextlib
import os
import signal
from types import SimpleNamespace

import pytest

from ember_code.core.tools.managed_process import ManagedProcess, process_group_of
from ember_code.core.tools.process_registry import ProcessRegistry


@pytest.fixture
def no_process_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``os`` module shaped like Windows'."""
    monkeypatch.delattr(os, "getpgid", raising=False)
    monkeypatch.delattr(os, "killpg", raising=False)


class _Proc:
    """Minimal ``asyncio.subprocess.Process`` stand-in."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def _managed(proc: _Proc) -> ManagedProcess:
    return ManagedProcess(proc, "npm run dev", SimpleNamespace(registry=None))


class TestKill:
    def test_the_child_is_killed_where_there_are_no_process_groups(self, no_process_groups):
        """The regression. Before the fix this raised AttributeError out
        of ``kill`` and ``proc.kill()`` was never reached, so the
        process stayed alive and the caller saw a crash instead."""
        proc = _Proc()

        _managed(proc).kill()

        assert proc.killed is True

    def test_the_process_group_is_still_signalled_on_posix(self, monkeypatch):
        """The guard is not a silent downgrade of POSIX behaviour: where
        process groups exist, the group still gets SIGTERM first so the
        whole tree dies, not just the ``/bin/sh``."""
        if not hasattr(os, "killpg"):  # pragma: no cover — POSIX-only run
            pytest.skip("this platform has no killpg, which the Windows tests cover")
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
        proc = _Proc(pid=4242)

        _managed(proc).kill()

        assert sent == [(4243, signal.SIGTERM)]
        assert proc.killed is True

    def test_a_dead_process_group_does_not_stop_the_direct_kill(self, monkeypatch):
        """``ProcessLookupError`` from the group leg — a race where the
        tree exited between the check and the signal — must still fall
        through to ``proc.kill()``."""
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)

        def _boom(pgid, sig):
            raise ProcessLookupError(pgid)

        monkeypatch.setattr(os, "killpg", _boom)
        proc = _Proc()

        _managed(proc).kill()

        assert proc.killed is True

    def test_kill_all_does_not_abort_halfway(self, no_process_groups):
        """``ProcessRegistry.kill_all`` kills inside its lock and clears
        the map afterwards. An AttributeError from the first entry took
        the loop, the remaining processes, and the clear() with it."""
        registry = ProcessRegistry()
        procs = [_Proc(pid=100), _Proc(pid=200), _Proc(pid=300)]
        for proc in procs:
            registry.add(_managed(proc))

        assert registry.kill_all() == 3
        assert all(p.killed for p in procs)
        assert registry.all_running() == []


class TestProcessGroupOf:
    def test_returns_none_where_the_platform_has_no_groups(self, no_process_groups):
        assert process_group_of(os.getpid()) is None

    def test_returns_none_for_a_pid_that_is_gone(self, monkeypatch):
        def _gone(pid):
            raise ProcessLookupError(pid)

        monkeypatch.setattr(os, "getpgid", _gone)

        assert process_group_of(999_999) is None

    def test_returns_the_real_group_on_posix(self):
        if not hasattr(os, "getpgid"):  # pragma: no cover — POSIX-only run
            pytest.skip("this platform has no getpgid")

        assert process_group_of(os.getpid()) == os.getpgid(os.getpid())


class TestBackgroundSpawnPersist:
    def test_announce_start_survives_a_missing_getpgid(self, no_process_groups):
        """The background-spawn hole. ``announce_start`` →
        ``_persist_add`` → ``os.getpgid`` raised past an
        ``except (ProcessLookupError, OSError)`` and up through
        ``ProcessSupervisor.run_backgrounded``, so no server, build, or
        watcher could be started at all. The row is persisted with
        ``pgid=None``, which is the honest value where there are no
        process groups."""
        rows: list[object] = []

        class _Store:
            async def upsert(self, row):
                rows.append(row)

        registry = ProcessRegistry(persistence=_Store())
        mp = _managed(_Proc(pid=777))
        registry.add(mp)

        registry.announce_start(mp)  # must not raise

    def test_suppression_alone_would_not_have_saved_it(self, no_process_groups):
        """Pins the mechanism, because "but it's already guarded" is the
        reading that let both call sites survive review. Neither the
        ``suppress`` form nor the ``except (ProcessLookupError, OSError)``
        form names ``AttributeError``."""
        with pytest.raises(AttributeError), contextlib.suppress(ProcessLookupError, OSError):
            os.killpg  # noqa: B018

        with pytest.raises(AttributeError):
            try:  # noqa: SIM105 — the try/except form IS the thing under test
                os.getpgid(1)
            except (ProcessLookupError, OSError):  # pragma: no cover — never reached
                pass
