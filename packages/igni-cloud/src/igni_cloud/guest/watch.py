"""Being told a file was written or renamed into the run directory, on Linux.

``docs/CLOUD.md`` §9. The node agent hands igni its producer by writing
``/run/igni/producer.env`` the atomic way — a temporary file, then a
rename — which inotify reports on the directory as ``IN_MOVED_TO``.
Watching for it makes binding as quick as the rename instead of up to a
poll interval late. No dependency offers inotify, so this is the three
libc calls through :mod:`ctypes`, read on the event loop with
``add_reader``; where they are missing (macOS, a sandbox without
inotify) :meth:`DirectoryWatch.start` says so and the caller polls.

One directory, one watch: igni waits on several files of the run
directory (``producer.env``, ``restored``, ``suspending``), and every
file written there — its own ``activity.json`` included — is an event.
Each waiter is a :class:`FileWatch` on one name, woken only by events
naming its file (``docs/CLOUD_RESUME_TODO.md`` item 1: on a restore,
only the restore watch runs; the binding and the suspend watch are not
woken to look at files that did not change, and touch nothing).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import logging
import os
import struct
import sys
import time
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)


class FileWatch:
    """Wakes its waiter when one file of a watched directory is written or renamed in."""

    def __init__(self, directory: DirectoryWatch, name: str) -> None:
        self.directory = directory
        self.name = name
        self._woken = asyncio.Event()
        #: ``time.perf_counter()`` when the event naming the file was read, until the waiter took it.
        self.woken_at: float | None = None

    @property
    def watching(self) -> bool:
        return self.directory.watching

    def start(self) -> bool:
        """Begin watching; False when inotify is not to be had here."""
        return self.directory.start()

    def wake(self, at: float) -> None:
        if self.woken_at is None:
            self.woken_at = at
        self._woken.set()

    async def wait(self, timeout: float | None) -> bool:
        """Until the file changed, or ``timeout`` (``None``: no limit). True when woken."""
        try:
            await asyncio.wait_for(self._woken.wait(), timeout)
        except TimeoutError:
            return False
        finally:
            self._woken.clear()
        return True

    def close(self) -> None:
        """Stop waiting; the directory's watch ends with its last file."""
        self.directory.release(self)


class DirectoryWatch:
    """One inotify watch on a directory; wakes the :class:`FileWatch` each event names."""

    IN_CLOSE_WRITE = 0x00000008
    IN_MOVED_TO = 0x00000080
    IN_CREATE = 0x00000100
    #: Events were dropped: every file may have changed.
    IN_Q_OVERFLOW = 0x00004000
    IN_NONBLOCK = 0o4000
    IN_CLOEXEC = 0o2000000
    MASK = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE
    #: ``struct inotify_event`` before its name: wd, mask, cookie, len.
    _EVENT: ClassVar[struct.Struct] = struct.Struct("iIII")

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._fd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._woken = asyncio.Event()
        self._unavailable = False
        self._files: dict[str, list[FileWatch]] = {}

    @property
    def watching(self) -> bool:
        return self._fd is not None

    def file(self, name: str) -> FileWatch:
        """A waiter woken only by events naming ``name`` in this directory."""
        watch = FileWatch(self, name)
        self._files.setdefault(name, []).append(watch)
        return watch

    def release(self, watch: FileWatch) -> None:
        waiters = self._files.get(watch.name, [])
        if watch in waiters:
            waiters.remove(watch)
            if not waiters:
                del self._files[watch.name]
        if not self._files:
            self.close()

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
        at = time.perf_counter()
        names: set[str] = set()
        everything = False
        with contextlib.suppress(BlockingIOError, InterruptedError):
            while data := os.read(self._fd, 4096):
                offset = 0
                while offset + self._EVENT.size <= len(data):
                    _wd, mask, _cookie, length = self._EVENT.unpack_from(data, offset)
                    offset += self._EVENT.size
                    raw = data[offset : offset + length]
                    offset += length
                    if mask & self.IN_Q_OVERFLOW:
                        everything = True
                    else:
                        names.add(os.fsdecode(raw.rstrip(b"\0")))
        self._woken.set()
        for name, waiters in list(self._files.items()):
            if everything or name in names:
                for waiter in waiters:
                    waiter.wake(at)

    async def wait(self, timeout: float | None) -> bool:
        """Until anything changed in the directory, or ``timeout`` (``None``: no limit). True when woken."""
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


__all__ = ["DirectoryWatch", "FileWatch"]
