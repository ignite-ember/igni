"""What igni did after a restore, step by step, until its first answer.

``docs/CLOUD_RESUME_TODO.md`` item 1. A resume is judged by how soon igni
answers the first request; on a restored VM that is mostly memory coming
back page by page, which a profile from outside the guest cannot see.
So igni times it itself: from noticing ``/run/igni/restored`` through
each after-restore step, the first window attaching, the first answer it
sends and the steps that waited for it, with the guest's page faults
and igni's CPU time each step took (``getrusage``) and any collection
that ran. One log line per restore — ``resume timeline``, the steps
under ``timeline`` — which the node collects with the rest of igni's
standard output.

Where the time before noticing went is split three ways: the guest
agent's ``at_ms`` is the host's clock when it sent ``post_restore``, so
``written_after_ms`` is the host's message and the agent's own steps up
to writing ``restored`` (the file's modification time, at the kernel's
tick), ``woke_after_write_ms`` is igni's event loop reading the
directory's event (long when the loop was held or the process not
running), and the rest is the loop reaching the restore watch.

Marking a step is a ``getrusage`` and a tuple: the records are built
when the timeline is logged, after the answer, so the timeline itself
adds next to nothing to the first answer's path.

A page the host has not prefetched yet is read on its first touch, in
the host's fault handler: the guest's own counters do not see that. With
``IGNI_RESUME_PAGES=1`` the timeline also clears the process's
referenced bits when it notices the restore and reads, after each step,
how much memory has been touched since (``/proc/self/smaps_rollup``):
what a cold resume reads for that step. Off by default — reading
``smaps_rollup`` walks every mapping, a cost of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import os
import resource
import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ResumeStep(BaseModel):
    """One step, measured from the moment the restore was noticed."""

    step: str
    #: When it ended, after the restore was noticed.
    at_ms: float
    #: How long it took itself (from the step before).
    took_ms: float
    minor_faults: int
    major_faults: int
    #: igni's CPU time since the restore was noticed (every thread's): far below
    #: ``at_ms``, igni was waiting — for the host, the guest's scheduler or a peer.
    cpu_ms: float = 0.0
    #: Memory touched since the restore was noticed (``IGNI_RESUME_PAGES=1``).
    touched_kb: int | None = None


class ResumeTimelineRecord(BaseModel):
    generation: int
    #: From the guest agent's ``at_ms`` (the host's clock at ``post_restore``) to igni noticing.
    noticed_after_ms: float | None = None
    #: From ``at_ms`` to the agent writing ``restored`` (its modification time).
    written_after_ms: float | None = None
    #: From ``restored`` written to igni's loop reading the directory's event.
    woke_after_write_ms: float | None = None
    clock_step_ms: float = 0.0
    steps: list[ResumeStep] = Field(default_factory=list)
    #: Ended by the first answer, or by the deadline with none sent.
    answered: bool = False


class ResumeTimeline:
    """Marks steps after one restore; logs them once its steps ran and it answered."""

    PAGES_ENV: ClassVar[str] = "IGNI_RESUME_PAGES"
    #: A restore nobody connects after is logged anyway, this long after.
    DEADLINE_SECONDS: ClassVar[float] = 10.0
    _SMAPS: ClassVar[Path] = Path("/proc/self/smaps_rollup")
    _CLEAR_REFS: ClassVar[Path] = Path("/proc/self/clear_refs")

    def __init__(
        self,
        generation: int,
        *,
        at_ms: int = 0,
        clock_step_ms: float = 0.0,
        written_ns: int | None = None,
        woken_at: float | None = None,
    ) -> None:
        self._started = time.perf_counter()
        now_ms = time.time() * 1000
        self._last = self._started
        self._pages = os.environ.get(self.PAGES_ENV) == "1"
        if self._pages:
            self._clear_referenced()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self._base = (usage.ru_minflt, usage.ru_majflt, usage.ru_utime + usage.ru_stime)
        self._generation = generation
        self._clock_step_ms = clock_step_ms
        self._noticed_after = round(now_ms - at_ms, 1) if at_ms else None
        written_ms = written_ns / 1e6 if written_ns else None
        self._written_after = (
            round(written_ms - at_ms, 1) if written_ms is not None and at_ms else None
        )
        woke_ms = now_ms - (self._started - woken_at) * 1000 if woken_at is not None else None
        self._woke_after_write = (
            round(woke_ms - written_ms, 1)
            if woke_ms is not None and written_ms is not None
            else None
        )
        #: step, at, took, minor faults, major faults, cpu ms, touched kB
        self._steps: list[tuple[str, float, float, int, int, float, int | None]] = []
        self._answered = False
        self._done = False
        self._steps_done = False
        gc.callbacks.append(self._collected)
        self._deadline: asyncio.TimerHandle | None = None
        with contextlib.suppress(RuntimeError):
            self._deadline = asyncio.get_running_loop().call_later(
                self.DEADLINE_SECONDS, self.finish
            )

    @property
    def done(self) -> bool:
        return self._done

    @property
    def record(self) -> ResumeTimelineRecord:
        """What is logged: the steps so far, as records."""
        return ResumeTimelineRecord(
            generation=self._generation,
            noticed_after_ms=self._noticed_after,
            written_after_ms=self._written_after,
            woke_after_write_ms=self._woke_after_write,
            clock_step_ms=self._clock_step_ms,
            steps=[
                ResumeStep(
                    step=step,
                    at_ms=round(at, 2),
                    took_ms=round(took, 2),
                    minor_faults=minor,
                    major_faults=major,
                    cpu_ms=round(cpu, 2),
                    touched_kb=touched,
                )
                for step, at, took, minor, major, cpu, touched in self._steps
            ],
            answered=self._answered,
        )

    def mark(self, step: str) -> None:
        if self._done:
            return
        now = time.perf_counter()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self._steps.append(
            (
                step,
                (now - self._started) * 1000,
                (now - self._last) * 1000,
                usage.ru_minflt - self._base[0],
                usage.ru_majflt - self._base[1],
                (usage.ru_utime + usage.ru_stime - self._base[2]) * 1000,
                self._touched_kb() if self._pages else None,
            )
        )
        self._last = now

    def answered(self) -> None:
        """The first answer went out; complete once the after-restore steps are too."""
        if self._done or self._answered:
            return
        self.mark("first answer")
        self._answered = True
        if self._steps_done:
            self.finish()

    def steps_done(self) -> None:
        """Every after-restore step ran; complete once an answer went out too."""
        self._steps_done = True
        if self._answered:
            self.finish()

    def _collected(self, phase: str, info: dict[str, Any]) -> None:
        """A collection during the timeline is a step of its own: it walks memory."""
        if phase == "start":
            self.mark("before a collection")
        else:
            self.mark(f"collection of generation {info.get('generation')}")

    def finish(self) -> None:
        if self._done:
            return
        self._done = True
        with contextlib.suppress(ValueError):
            gc.callbacks.remove(self._collected)
        if self._deadline is not None:
            self._deadline.cancel()
        logger.info(
            "resume timeline",
            extra={"fields": {"timeline": self.record.model_dump(mode="json")}},
        )

    @classmethod
    def _clear_referenced(cls) -> None:
        try:
            cls._CLEAR_REFS.write_text("1")
        except OSError as exc:
            logger.debug("cannot clear referenced bits: %s", exc)

    @classmethod
    def _touched_kb(cls) -> int | None:
        try:
            for line in cls._SMAPS.read_text().splitlines():
                if line.startswith("Referenced:"):
                    return int(line.split()[1])
        except (OSError, ValueError):
            return None
        return None


__all__ = ["ResumeStep", "ResumeTimeline", "ResumeTimelineRecord"]
