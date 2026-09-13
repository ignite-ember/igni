"""Keep the machine awake while the agent has work running.

An agent that starts a twenty-minute build and waits for it loses the
build if the machine idle-sleeps in the meantime. This holds a power
assertion for exactly as long as there is at least one tracked
background process, and drops it when the last one finishes.

What this can and cannot do, on macOS:

* **Idle sleep — prevented.** The machine will not fall asleep on its
  own while work is in flight, on battery or on mains.
* **Lid closed — not prevented, and not preventable.** Closing the lid
  on an Apple laptop sleeps the machine regardless of any assertion a
  normal process can hold. The exception is clamshell mode, which
  needs external power *and* an external display or keyboard, and is
  the OS's decision rather than something an application can ask for.
  ``caffeinate -s`` does not change this; per its own manual it is
  "valid only when system is running on AC power", and it addresses
  system sleep, not the lid.

So: walking away from an open laptop is safe. Closing it is not, and
no amount of code here changes that — it needs `pmset` configuration
or clamshell mode, both of which are the user's to decide.

Implemented with ``caffeinate`` rather than an ``IOPMAssertion`` FFI
binding: it ships with macOS, it is the documented interface, and
``-w <pid>`` makes the OS release the assertion if this process dies
without cleaning up — which a ctypes assertion would leak until
reboot.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)


class SleepBlocker:
    """Refcounted "keep awake" assertion.

    :meth:`acquire` and :meth:`release` are called from the process
    registry as background processes come and go. The first acquire
    starts ``caffeinate``; the last release stops it. Both are safe to
    call from any thread — the registry holds its own lock and fires
    these from a reader task.

    A no-op on anything that is not macOS, and on a macOS without
    ``caffeinate`` on PATH, so callers never need to ask.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._popen: subprocess.Popen[bytes] | None = None

    @property
    def supported(self) -> bool:
        return sys.platform == "darwin" and shutil.which("caffeinate") is not None

    @property
    def holding(self) -> bool:
        """True while an assertion is actually held."""
        return self._popen is not None and self._popen.poll() is None

    def acquire(self) -> None:
        """Take a reference; start the assertion if this is the first."""
        if not self.supported:
            return
        with self._lock:
            self._count += 1
            if self._count > 1 or self.holding:
                return
            self._start()

    def release(self) -> None:
        """Drop a reference; stop the assertion when it hits zero.

        Clamps at zero rather than going negative: a double release
        should not leave the machine pinned awake by a count that can
        never come back down.
        """
        if not self.supported:
            return
        with self._lock:
            if self._count == 0:
                return
            self._count -= 1
            if self._count == 0:
                self._stop()

    def _start(self) -> None:
        try:
            # ``-i`` prevents idle system sleep. ``-w`` ties the
            # assertion's lifetime to this process, so a crash here
            # cannot leave the machine awake indefinitely.
            self._popen = subprocess.Popen(
                ["caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.debug("sleep blocker: holding (caffeinate pid=%s)", self._popen.pid)
        except Exception:  # noqa: BLE001 — staying awake is best-effort
            logger.warning("sleep blocker: could not start caffeinate", exc_info=True)
            self._popen = None

    def _stop(self) -> None:
        popen = self._popen
        self._popen = None
        if popen is None or popen.poll() is not None:
            return
        with contextlib.suppress(Exception):
            popen.terminate()
        logger.debug("sleep blocker: released")
