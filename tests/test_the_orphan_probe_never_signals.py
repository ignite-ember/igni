"""``OrphanProcess.probe_alive`` asks; it must not send anything.

It inlined ``os.kill(pid, 0)``. On POSIX that is the textbook probe. On
Windows it is not a probe at all: CPython routes signal 0 to
``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``, which normally fails
with a plain ``OSError`` — so every live process read as dead and
``OrphanRehydrator.run`` stamped every re-adoptable background process
as interrupted — and, when the pid happened to name a console process
group, delivered a real Ctrl-C to a process we were only asking about.

``ParentProcessProbe`` (backend/supervisor.py) already owned the tested
per-platform answer; this file pins that ``probe_alive`` is that answer
and not a second, divergent one.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from ember_code.core.tools.orphan_process import OrphanProcess


@pytest.fixture
def kill_spy(monkeypatch):
    """Record every ``os.kill`` instead of delivering it.

    Also stops a POSIX test host from signalling the pytest process when
    the probe is pointed at a pid that happens to be ours.
    """
    calls: list[tuple] = []
    monkeypatch.setattr(os, "kill", lambda *args: calls.append(args))
    return calls


def test_the_windows_branch_never_signals_the_process_it_asks_about(monkeypatch, kill_spy):
    monkeypatch.setattr(sys, "platform", "win32")

    OrphanProcess.probe_alive(4242)

    assert kill_spy == [], (
        "probe_alive called os.kill on Windows — signal 0 there is CTRL_C_EVENT, "
        "so the 'probe' interrupts the background process it was asking about"
    )


def test_the_windows_branch_errs_toward_keeping_the_row(monkeypatch):
    """A broken probe must not report a live process dead.

    On a POSIX host the Windows branch cannot load ``kernel32`` and takes
    its own ``(AttributeError, OSError)`` fallback, which answers
    "alive". That is the answer rehydration needs: a false "dead" marks a
    perfectly healthy background process as interrupted.
    """
    monkeypatch.setattr(sys, "platform", "win32")

    assert OrphanProcess.probe_alive(4242) is True


def test_a_live_pid_reads_as_alive_on_posix():
    assert OrphanProcess.probe_alive(os.getpid()) is True


def test_a_reaped_pid_reads_as_dead_on_posix():
    # ``wait`` reaps, so the pid is fully gone by the time we probe it —
    # no zombie to mistake for a live process.
    proc = subprocess.Popen(["true"])
    pid = proc.pid
    proc.wait()

    assert OrphanProcess.probe_alive(pid) is False


def test_a_zero_or_negative_pid_is_never_alive():
    # ``os.kill(0, 0)`` signals the caller's whole process group, so the
    # inlined version answered "alive" for a pid that names no process.
    assert OrphanProcess.probe_alive(0) is False
    assert OrphanProcess.probe_alive(-1) is False
