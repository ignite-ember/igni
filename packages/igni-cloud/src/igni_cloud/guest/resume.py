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

Nothing waits for this: a request is answered whether or not the
restore has been noticed — a client connecting is proof enough that the
VM runs. What has to happen before a query (:attr:`RestoreStep.AT_ONCE`)
is only marking the database pools' connections stale, which touches
nothing. The reseed of Python's own generators runs the moment the first
answer has gone out (:attr:`RestoreStep.AT_FIRST_ANSWER`; nothing on the
first-answer path draws from them, and the kernel's randomness was
reseeded before the VM ran). The rest — the notification stream, the
timers, the activity file, the credential — waits also for the burst of
requests a window sends with its first (:attr:`RestoreWatch.AFTER_ANSWER_SECONDS`),
or :attr:`RestoreWatch.LATER_SECONDS` if nobody asks: on a restored VM
every page touched before an answer is a page that answer waits for.
Each restore's steps are timed (:mod:`igni_cloud.guest.timeline`).

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
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ember_code.core.activity import background
from igni_cloud.guest.timeline import ResumeTimeline
from igni_cloud.guest.watch import DirectoryWatch

logger = logging.getLogger(__name__)

#: Told the restore it follows.
RestoreCallback = Callable[["Restored"], Awaitable[None]]


class Restored(BaseModel):
    """What the guest agent writes after a restore."""

    generation: int
    at_ms: int = 0
    clock_step_ms: float = 0.0


class RestoreStep(StrEnum):
    """When an after-restore step runs."""

    #: As soon as the restore is noticed, before anything is answered.
    AT_ONCE = "at_once"
    #: As soon as the first answer has gone out (or nobody asked for a moment).
    AT_FIRST_ANSWER = "at_first_answer"
    #: Once the burst of requests a window sends with it is through.
    LATER = "later"


class RestoreWatch:
    """Runs its callbacks once per restore the guest agent reports."""

    #: Between looks where the directory cannot be watched (development).
    POLL_SECONDS = 1.0
    #: The steps that wait for the first answer run by then if none came.
    LATER_SECONDS = 1.0
    #: And this long after it: a window's first requests come as a burst.
    AFTER_ANSWER_SECONDS = 0.5

    def __init__(self, path: Path, *, watch: bool = True) -> None:
        self.path = path
        self._directory = DirectoryWatch(path.parent) if watch else None
        self._steps_by: dict[RestoreStep, list[RestoreCallback]] = {
            when: [] for when in RestoreStep
        }
        self._answered = asyncio.Event()
        self._waiting = False
        self._generation: int | None = None
        self._task: asyncio.Future[None] | None = None
        #: The last restore's, until its first answer (:mod:`igni_cloud.guest.timeline`).
        self.timeline: ResumeTimeline | None = None

    def on_restore(
        self, callback: RestoreCallback, *, when: RestoreStep = RestoreStep.AT_ONCE
    ) -> None:
        """Run ``callback`` after every restore, at ``when``."""
        self._steps_by[when].append(callback)

    @property
    def waiting_for_answer(self) -> bool:
        """A restore's later steps are waiting for igni's first answer."""
        return self._waiting

    def answered(self) -> None:
        """igni sent its first answer since the restore: the waiting steps may run."""
        self._answered.set()
        if self.timeline is not None:
            self.timeline.answered()

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
        if self.timeline is not None:
            self.timeline.finish()
        self._answered.clear()
        timeline = self.timeline = ResumeTimeline(
            restored.generation, at_ms=restored.at_ms, clock_step_ms=restored.clock_step_ms
        )
        await self._steps(self._steps_by[RestoreStep.AT_ONCE], restored, timeline)
        at_answer = self._steps_by[RestoreStep.AT_FIRST_ANSWER]
        later = self._steps_by[RestoreStep.LATER]
        if at_answer or later:
            self._waiting = True
            try:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._answered.wait(), self.LATER_SECONDS)
                await self._steps(at_answer, restored, timeline)
                if later and self._answered.is_set():
                    await asyncio.sleep(self.AFTER_ANSWER_SECONDS)
            finally:
                self._waiting = False
            await self._steps(later, restored, timeline)
        timeline.steps_done()
        return restored

    @staticmethod
    async def _steps(
        callbacks: list[RestoreCallback], restored: Restored, timeline: ResumeTimeline
    ) -> None:
        for callback in list(callbacks):
            try:
                await callback(restored)
            except Exception:
                logger.exception("an after-restore step failed")
            timeline.mark(getattr(callback, "__name__", "a step"))

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


__all__ = ["RestoreCallback", "RestoreStep", "RestoreWatch", "Restored"]
