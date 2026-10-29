"""igni's steps before a suspend: its database connections closed, freed heap handed back.

``docs/CLOUD_RESUME_TODO.md`` item 2, ``igni__cloudvm``'s
``docs/guest-protocol.md`` (``before_suspend``). The node agent asks the
guest agent to run ``hooks/before-suspend.d/*`` before every suspend;
igni's hook (``packages/igni-cloud/hooks/before-suspend.d/50-igni``)
writes ``/run/igni/suspending`` (``{"token"}``, atomically) and waits —
half a second at most — for igni to answer in ``/run/igni/suspend-ready``
with the same token. In between igni runs its before-suspend steps
(:meth:`SuspendWatch.before_suspend`) — the cloud's closes every
PostgreSQL connection it holds, Agno's included, so a paused VM holds
none: a node pauses about a thousand, and each producer's own login
cannot share a pooler; the first query after the VM runs again opens a
fresh one — and then, when its allocator holds a lot free, returns that
memory to the kernel (``malloc_trim(0)``): pages the kernel then reports
free are not part of the snapshot, so a cold resume does not read them.

Only a lot (:attr:`HeapTrim.MIN_FREE_KB`): a trimmed page that igni
uses again comes back as a fresh page — in the VM a guest page fault and
a host fault for a zero page each, never prefetched with the working
set. Trimming before every suspend cost every wake's first requests a
page fault per page the allocator gave back (diagnostic, Linux container,
fake model, six turns of 300 kB replies: the first database answer after
a wake took ~3,900 page faults with the trim, 5 without; on the image,
counted, ~3,200–3,500 guest page faults over ~255 ms after every wake's
first answer, from paused as from a snapshot) to save
~2 MB of changed pages per suspend. Free heap of a usual turn's size is
the next turn's: kept. Free heap of a peak far beyond it (a big run's
leftovers) goes back. A paused VM holds what is kept (here 19–45 MB
after six turns of 300 kB replies): latency against density, a
setting to weigh on the node's counted runs.

Watched like ``restored`` (inotify on the run directory): no timer.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ember_code.core.activity import background
from igni_cloud.guest.files import AtomicFile
from igni_cloud.guest.watch import DirectoryWatch, FileWatch

logger = logging.getLogger(__name__)


class SuspendRequest(BaseModel):
    token: str


class SuspendReady(BaseModel):
    token: str
    ms: float
    #: Steps that did not finish in their share of the hook's half second.
    unfinished: list[str] = Field(default_factory=list)
    #: How much the resident set shrank (0: the free heap was kept); ``None`` where the heap is not glibc's.
    trimmed_kb: int | None = None
    #: What the allocator held free before (``mallinfo2``); ``None`` where it cannot say.
    free_heap_kb: int | None = None


class MallInfo2(ctypes.Structure):
    """glibc's ``struct mallinfo2``."""

    _fields_ = [
        (name, ctypes.c_size_t)
        for name in (
            "arena",
            "ordblks",
            "smblks",
            "hblks",
            "hblkhd",
            "usmblks",
            "fsmblks",
            "uordblks",
            "fordblks",
            "keepcost",
        )
    ]


class HeapTrim:
    """glibc's ``malloc_trim(0)``: free heap pages given back to the kernel, when there are many."""

    #: Free heap below this is kept for the next turn (see the module's notes).
    MIN_FREE_KB = 64 * 1024
    _STATM = Path("/proc/self/statm")

    @classmethod
    def _libc(cls) -> ctypes.CDLL | None:
        if not sys.platform.startswith("linux"):
            return None
        try:
            return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        except OSError:
            return None

    @classmethod
    def free_kb(cls) -> int | None:
        """What the allocator holds free (``mallinfo2().fordblks``); ``None`` where it cannot say."""
        libc = cls._libc()
        try:
            info = libc.mallinfo2 if libc is not None else None
        except AttributeError:
            return None
        if info is None:
            return None
        info.restype = MallInfo2
        return int(info().fordblks) // 1024

    @classmethod
    def run(cls, free_kb: int | None = None) -> int | None:
        """Trim when ``free_kb`` (unknown: always) reaches :attr:`MIN_FREE_KB`.

        The resident set's shrink in kB — 0 when the heap was kept — or
        ``None`` with no glibc here.
        """
        libc = cls._libc()
        try:
            trim = libc.malloc_trim if libc is not None else None
        except AttributeError:
            return None
        if trim is None:
            return None
        if free_kb is not None and free_kb < cls.MIN_FREE_KB:
            return 0
        before = cls._resident_kb()
        trim(0)
        after = cls._resident_kb()
        return max(0, before - after) if before is not None and after is not None else None

    @classmethod
    def _resident_kb(cls) -> int | None:
        try:
            pages = int(cls._STATM.read_text().split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return pages * os.sysconf("SC_PAGE_SIZE") // 1024


class SuspendWatch:
    """Answers each ``suspending`` request once, after getting ready."""

    #: Between looks where the directory cannot be watched (development).
    POLL_SECONDS = 1.0
    #: The most the steps may take together: the hook waits half a second.
    STEPS_SECONDS = 0.4

    def __init__(
        self,
        request: Path,
        ready: Path,
        *,
        watch: bool = True,
        directory: DirectoryWatch | None = None,
    ) -> None:
        self.request = request
        self.ready = ready
        #: Woken by ``suspending`` alone; ``directory`` shares one watch of the run directory.
        self._directory: FileWatch | None = (
            (directory or DirectoryWatch(request.parent)).file(request.name) if watch else None
        )
        self._answered: str | None = None
        self._task: asyncio.Future[None] | None = None
        self._steps: list[Callable[[], Awaitable[None]]] = []

    def before_suspend(self, step: Callable[[], Awaitable[None]]) -> None:
        """Run ``step`` before answering each request, ahead of the heap trim."""
        self._steps.append(step)

    def read(self) -> SuspendRequest | None:
        try:
            return SuspendRequest.model_validate(json.loads(self.request.read_text("utf-8")))
        except (OSError, ValueError, ValidationError):
            return None

    async def check(self) -> SuspendReady | None:
        """Look once; get ready and answer a request not answered yet."""
        request = self.read()
        if request is None or request.token == self._answered:
            return None
        started = time.perf_counter()
        unfinished: list[str] = []
        for step in list(self._steps):
            remaining = self.STEPS_SECONDS - (time.perf_counter() - started)
            name = getattr(step, "__name__", "a step")
            try:
                await asyncio.wait_for(step(), max(remaining, 0.01))
            except TimeoutError:
                unfinished.append(name)
            except Exception:
                logger.exception("before-suspend step %s failed", name)
                unfinished.append(name)
        free = HeapTrim.free_kb()
        trimmed = HeapTrim.run(free)
        answer = SuspendReady(
            token=request.token,
            ms=round((time.perf_counter() - started) * 1000, 2),
            trimmed_kb=trimmed,
            free_heap_kb=free,
            unfinished=unfinished,
        )
        try:
            AtomicFile(self.ready).write(answer.model_dump_json().encode())
        except OSError as exc:
            logger.warning("cannot answer %s: %s", self.request, exc)
            return None
        self._answered = request.token
        logger.info(
            "ready to suspend: %s kB trimmed of %s kB free in %.1f ms", trimmed, free, answer.ms
        )
        return answer

    def start(self) -> None:
        if self._task is None:
            # A request left from before this igni (or this VM's base) is
            # not this suspend's.
            request = self.read()
            self._answered = request.token if request is not None else None
            self._task = background.daemon(self._run(), name="suspend-watch")

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


__all__ = ["HeapTrim", "MallInfo2", "SuspendReady", "SuspendRequest", "SuspendWatch"]
