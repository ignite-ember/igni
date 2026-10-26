"""Noticing a snapshot restore from inside the VM: ``/run/igni/restored``.

``docs/CLOUD.md`` §9. A suspended VM is frozen whole: igni's coroutines,
its timers, its open sockets. On restore every one of them carries on
where it was, but the world did not wait. The guest agent's
``post_restore`` steps the wall clock, reseeds the kernel and then
writes ``restored`` (``{"generation", "at_ms", "clock_step_ms"}``,
atomically; the generation grows with every restore). igni watches the
run directory (inotify, as for ``producer.env``) and, on a new
generation, runs its after-restore steps: pseudo-random state reseeded,
database pools emptied, the notification stream reopened, timers
re-armed from the wall clock (the monotonic clock did not move while the
VM slept, so an ``asyncio.sleep`` armed before is late by the whole
suspension), the activity file rewritten.

Not from a clock jump: a quick come-back steps the clock by less than any
threshold worth having. And no timer of its own: where inotify works the
watch sleeps until the directory changes, so an idle igni does no work
(every wake-up dirties memory the next suspend must write).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ember_code.backend.guest.watch import DirectoryWatch
from ember_code.core.activity import background

logger = logging.getLogger(__name__)

#: Told the restore it follows.
RestoreCallback = Callable[["Restored"], Awaitable[None]]


class Restored(BaseModel):
    """What the guest agent writes after a restore."""

    generation: int
    at_ms: int = 0
    clock_step_ms: float = 0.0


class RestoreWatch:
    """Runs its callbacks once per restore the guest agent reports."""

    #: Between looks where the directory cannot be watched (development).
    POLL_SECONDS = 1.0

    def __init__(self, path: Path, *, watch: bool = True) -> None:
        self.path = path
        self._directory = DirectoryWatch(path.parent) if watch else None
        self._callbacks: list[RestoreCallback] = []
        self._generation: int | None = None
        self._task: asyncio.Future[None] | None = None

    def on_restore(self, callback: RestoreCallback) -> None:
        self._callbacks.append(callback)

    def read(self) -> Restored | None:
        try:
            return Restored.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError):
            return None

    async def check(self) -> Restored | None:
        """Look once; run the callbacks for a generation not seen yet."""
        restored = self.read()
        if restored is None or restored.generation == self._generation:
            return None
        first_look = self._generation is None
        self._generation = restored.generation
        if first_look:
            # What was there when igni started watching: a restore before
            # it bound, which nothing of this igni's outlived.
            return None
        logger.info(
            "restored (generation %d, clock stepped %.0f ms)",
            restored.generation,
            restored.clock_step_ms,
        )
        for callback in list(self._callbacks):
            try:
                await callback(restored)
            except Exception:
                logger.exception("an after-restore step failed")
        return restored

    def start(self) -> None:
        if self._task is None:
            restored = self.read()
            self._generation = restored.generation if restored is not None else -1
            self._task = background.daemon(self._run(), name="restore-watch")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._directory is not None:
            self._directory.close()

    async def _run(self) -> None:
        while True:
            directory = self._directory
            if directory is not None and (directory.watching or directory.start()):
                await directory.wait(None)
            else:
                await asyncio.sleep(self.POLL_SECONDS)
            await self.check()


__all__ = ["RestoreCallback", "RestoreWatch", "Restored"]
