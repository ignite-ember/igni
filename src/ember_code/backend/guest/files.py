"""Files the guest agent reads while igni writes them."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


class AtomicFile:
    """A file replaced whole: a reader sees the old content or the new, never half.

    The temporary file is made in the same directory, so the rename
    stays on one filesystem and is atomic; it is flushed to the file
    before the rename. The way the guest agent's ``write_file`` writes
    the files igni reads.
    """

    #: Readable by the guest agent whoever igni runs as.
    MODE = 0o644

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, data: bytes) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, self.MODE)
            os.replace(temp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise

    def remove(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


class ReadyFile:
    """``/run/igni/ready``: igni is listening, its imports done.

    Cleared when the process starts and when it stops, so a file left by
    an igni that has since exited never says a new one is serving. The
    node agent takes a warm base snapshot only after it appears.
    """

    def __init__(self, path: Path) -> None:
        self._file = AtomicFile(path)

    @property
    def path(self) -> Path:
        return self._file.path

    def mark(self) -> None:
        self._file.write(f"{os.getpid()}\n".encode())

    def clear(self) -> None:
        self._file.remove()


__all__ = ["AtomicFile", "ReadyFile"]
