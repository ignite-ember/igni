"""Noticing a snapshot restore from inside the VM.

``docs/CLOUD.md`` §9. A suspended VM is frozen whole: igni's coroutines,
its timers, its open sockets. On restore every one of them carries on
where it was, but the world did not wait. The guest agent's
``post_restore`` steps the wall clock to the host's (``CLOCK_REALTIME``)
while the monotonic clock carries on from where it froze — so a wall
clock that ran ahead of the monotonic one by more than a few seconds is
a restore, as seen from in here.

What igni does about it is the backend's (:class:`ResumeWatch` takes the
callbacks): the database pools are emptied, so no request waits on a
connection the server or the network dropped while the VM slept, and
the notification stream reconnects instead of sitting out its read
timeout on a dead socket. Everything igni times — the asyncio loop, the
session pool's idle sweep — runs on the monotonic clock, which a restore
does not move, so no timer fires early or in a burst; the scheduler's
due tasks are on the wall clock and run once each when the clock says
they are due, a recurring one resuming at its next occurrence rather
than replaying the ones it slept through.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Told how many seconds the VM was away.
ResumeCallback = Callable[[float], Awaitable[None]]


class ResumeWatch:
    """Calls its callbacks when the wall clock jumps ahead of the monotonic one."""

    #: How often the two clocks are compared.
    INTERVAL_SECONDS = 1.0
    #: A jump smaller than this is NTP slewing or a slow loop, not a restore.
    THRESHOLD_SECONDS = 5.0

    def __init__(
        self,
        *,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        interval: float | None = None,
        threshold: float | None = None,
    ) -> None:
        self._wall = wall
        self._monotonic = monotonic
        self._interval = self.INTERVAL_SECONDS if interval is None else interval
        self._threshold = self.THRESHOLD_SECONDS if threshold is None else threshold
        self._callbacks: list[ResumeCallback] = []
        self._last = (wall(), monotonic())
        self._task: asyncio.Task[None] | None = None

    def on_resume(self, callback: ResumeCallback) -> None:
        self._callbacks.append(callback)

    def jump(self) -> float:
        """Seconds the wall clock moved beyond the monotonic one since the last look."""
        wall, monotonic = self._wall(), self._monotonic()
        last_wall, last_monotonic = self._last
        self._last = (wall, monotonic)
        return (wall - last_wall) - (monotonic - last_monotonic)

    async def check(self) -> bool:
        """Look once; run the callbacks if the VM was away. True when it was."""
        away = self.jump()
        if away < self._threshold:
            return False
        logger.info("resumed after %.1f s away; reconnecting", away)
        for callback in list(self._callbacks):
            try:
                await callback(away)
            except Exception:
                logger.exception("an after-resume step failed")
        return True

    def start(self) -> None:
        if self._task is None:
            self._last = (self._wall(), self._monotonic())
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.check()


__all__ = ["ResumeCallback", "ResumeWatch"]
