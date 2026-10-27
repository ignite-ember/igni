"""igni's step before a suspend: freed heap memory handed back.

``docs/CLOUD_RESUME_TODO.md`` item 2, ``igni__cloudvm``'s
``docs/guest-protocol.md`` (``before_suspend``). The node agent asks the
guest agent to run ``hooks/before-suspend.d/*`` before every suspend;
igni's hook (``packages/igni-cloud/hooks/before-suspend.d/50-igni``)
writes ``/run/igni/suspending`` (``{"token"}``, atomically) and waits —
half a second at most — for igni to answer in ``/run/igni/suspend-ready``
with the same token. In between igni returns the memory its allocator
holds free to the kernel (``malloc_trim(0)``): pages the kernel then
reports free are not part of the snapshot, so a cold resume does not
read them. Measured in a Linux container after a turn (diagnostic):
about 2 MB fewer changed pages.

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
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ember_code.core.activity import background
from igni_cloud.guest.files import AtomicFile
from igni_cloud.guest.watch import DirectoryWatch

logger = logging.getLogger(__name__)


class SuspendRequest(BaseModel):
    token: str


class SuspendReady(BaseModel):
    token: str
    ms: float
    #: How much the resident set shrank; ``None`` where the heap is not glibc's.
    trimmed_kb: int | None = None


class HeapTrim:
    """glibc's ``malloc_trim(0)``: free heap pages given back to the kernel."""

    _STATM = Path("/proc/self/statm")

    @classmethod
    def run(cls) -> int | None:
        """Trim; the resident set's shrink in kB, or ``None`` with no glibc here."""
        if not sys.platform.startswith("linux"):
            return None
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            trim = libc.malloc_trim
        except (OSError, AttributeError):
            return None
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

    def __init__(self, request: Path, ready: Path, *, watch: bool = True) -> None:
        self.request = request
        self.ready = ready
        self._directory = DirectoryWatch(request.parent) if watch else None
        self._answered: str | None = None
        self._task: asyncio.Future[None] | None = None

    def read(self) -> SuspendRequest | None:
        try:
            return SuspendRequest.model_validate(json.loads(self.request.read_text("utf-8")))
        except (OSError, ValueError, ValidationError):
            return None

    def check(self) -> SuspendReady | None:
        """Look once; get ready and answer a request not answered yet."""
        request = self.read()
        if request is None or request.token == self._answered:
            return None
        started = time.perf_counter()
        trimmed = HeapTrim.run()
        answer = SuspendReady(
            token=request.token,
            ms=round((time.perf_counter() - started) * 1000, 2),
            trimmed_kb=trimmed,
        )
        try:
            AtomicFile(self.ready).write(answer.model_dump_json().encode())
        except OSError as exc:
            logger.warning("cannot answer %s: %s", self.request, exc)
            return None
        self._answered = request.token
        logger.info("ready to suspend: %s kB trimmed in %.1f ms", trimmed, answer.ms)
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
            self.check()


__all__ = ["HeapTrim", "SuspendReady", "SuspendRequest", "SuspendWatch"]
