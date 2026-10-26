"""Being told a file was renamed into the run directory, on Linux.

``docs/CLOUD.md`` §9. The node agent hands igni its producer by writing
``/run/igni/producer.env`` the atomic way — a temporary file, then a
rename — which inotify reports on the directory as ``IN_MOVED_TO``.
Watching for it makes binding as quick as the rename instead of up to a
poll interval late. No dependency offers inotify, so this is the three
libc calls through :mod:`ctypes`, read on the event loop with
``add_reader``; where they are missing (macOS, a sandbox without
inotify) :meth:`DirectoryWatch.start` says so and the caller polls.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class DirectoryWatch:
    """Wakes its waiter when a file in one directory is created, written or renamed in."""

    IN_CLOSE_WRITE = 0x00000008
    IN_MOVED_TO = 0x00000080
    IN_CREATE = 0x00000100
    IN_NONBLOCK = 0o4000
    IN_CLOEXEC = 0o2000000
    MASK = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._fd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._woken = asyncio.Event()
        self._unavailable = False

    @property
    def watching(self) -> bool:
        return self._fd is not None

    def start(self) -> bool:
        """Begin watching; False when inotify is not to be had here."""
        if self._fd is not None:
            return True
        if self._unavailable or not sys.platform.startswith("linux"):
            return False
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            fd = libc.inotify_init1(self.IN_NONBLOCK | self.IN_CLOEXEC)
            if fd < 0:
                raise OSError(ctypes.get_errno(), "inotify_init1")
            watch = libc.inotify_add_watch(fd, os.fsencode(self.directory), self.MASK)
            if watch < 0:
                error = ctypes.get_errno()
                os.close(fd)
                raise OSError(error, f"inotify_add_watch {self.directory}")
            self._loop = asyncio.get_running_loop()
            self._loop.add_reader(fd, self._readable)
        except (OSError, AttributeError) as exc:
            logger.info("not watching %s (%s); polling it", self.directory, exc)
            self._unavailable = True
            return False
        self._fd = fd
        return True

    def _readable(self) -> None:
        assert self._fd is not None
        with contextlib.suppress(BlockingIOError, InterruptedError):
            while os.read(self._fd, 4096):
                pass
        self._woken.set()

    async def wait(self, timeout: float) -> bool:
        """Until something changed in the directory, or ``timeout``. True when woken."""
        try:
            await asyncio.wait_for(self._woken.wait(), timeout)
        except TimeoutError:
            return False
        finally:
            self._woken.clear()
        return True

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


__all__ = ["DirectoryWatch"]
