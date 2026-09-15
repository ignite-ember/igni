"""Notice that the machine slept, and tell whoever cares.

Nothing reacted to a wake. The backend survives sleep — processes are
frozen rather than killed, and exit detection is SIGCHLD rather than a
timer, so local work resumes correctly — but everything with a remote
peer does not. Sockets are closed from the far end while we are
frozen, so on wake the cloud state we hold is stale and nothing knows
it. The codeindex pill kept showing whatever it last resolved until
the next poll happened to fail.

**Detected here rather than in the desktop shell.** The obvious
implementation is ``NSWorkspaceDidWakeNotification`` in the Tauri app.
But this backend runs under four hosts — Tauri, VS Code, JetBrains,
and bare CLI — so a shell-side hook would serve one of them, on one
operating system. The clock does better:

* ``time.monotonic()`` does not advance while the machine is asleep.
* ``time.time()`` does.

So the gap between them *is* the sleep, on every platform and under
every host, with no new dependency and nothing to mock in a test but
two clocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

logger = logging.getLogger(__name__)

#: How often to compare the clocks. Short enough that a wake is
#: noticed before a user finishes opening the lid and looking at the
#: window; long enough to be free.
DEFAULT_POLL_SECONDS = 5.0

#: Below this, assume the loop was merely late — a busy event loop, a
#: long GC pause, a debugger breakpoint — rather than a sleep. Waking
#: subsystems for a 200 ms hiccup would make this a nuisance rather
#: than a signal.
DEFAULT_THRESHOLD_SECONDS = 30.0


class _Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall(self) -> float: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class WakeDetector:
    """Calls back when the wall clock jumps ahead of the monotonic one.

    Subscribers are coroutines. Each is awaited on wake and its
    failure is logged and swallowed: one subsystem's bad reaction to a
    wake must not stop the others from getting theirs, and none of
    them is important enough to take the backend down.
    """

    def __init__(
        self,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
        clock: _Clock | None = None,
    ) -> None:
        self._poll = poll_seconds
        self._threshold = threshold_seconds
        self._clock = clock or _SystemClock()
        self._subscribers: list[Callable[[float], Awaitable[None]]] = []
        self._task: asyncio.Task | None = None

    def subscribe(self, callback: Callable[[float], Awaitable[None]]) -> None:
        """Register a coroutine to run on wake, given the slept seconds."""
        self._subscribers.append(callback)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._loop(), name="wake-detector")
        return self._task

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def check_once(self, expected: float, wall_before: float, mono_before: float) -> float:
        """One comparison. Returns the seconds slept, or 0.0.

        Split out from the loop so the arithmetic can be tested
        without waiting for anything — the whole point of using
        clocks rather than a platform notification.
        """
        wall_elapsed = self._clock.wall() - wall_before
        mono_elapsed = self._clock.monotonic() - mono_before
        # The monotonic clock is the honest one; what the wall clock
        # saw beyond it is time the machine was not running.
        drift = wall_elapsed - mono_elapsed
        if drift < self._threshold:
            return 0.0
        logger.info(
            "wake: the machine appears to have slept for %.0fs "
            "(expected a %.0fs tick, wall clock moved %.0fs)",
            drift,
            expected,
            wall_elapsed,
        )
        await self._notify(drift)
        return drift

    async def _notify(self, slept: float) -> None:
        for callback in list(self._subscribers):
            try:
                await callback(slept)
            except Exception:  # noqa: BLE001 — one bad subscriber is not the others' problem
                logger.exception("wake: subscriber failed")

    async def _loop(self) -> None:
        wall = self._clock.wall()
        mono = self._clock.monotonic()
        while True:
            await asyncio.sleep(self._poll)
            await self.check_once(self._poll, wall, mono)
            wall = self._clock.wall()
            mono = self._clock.monotonic()
