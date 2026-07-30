"""ManagedProcess — one running asyncio subprocess and its output.

Extracted from :mod:`ember_code.core.tools.shell` per the OOP audit:
the class owns its lifecycle, buffers, reader task, and per-pid log
file — as instance methods, not free functions reaching into
``__slots__`` from three modules away.

Composition:

* :class:`ManagedProcess` takes a
  :class:`~ember_code.core.tools.process_supervisor.ProcessSupervisor`
  at construction time and calls back into
  ``self._supervisor.registry`` for the bus-emit / completion
  hooks (``emit_line`` / ``emit_completion``) — no module-level
  reach-in.
* The reader task is bound to the instance — ``self._reader`` — and
  gets started via :meth:`start_reader`.
* Eviction task handles live on
  :class:`~ember_code.core.tools.process_registry.ProcessRegistry`
  (indexed by pid) rather than as a slot on this class — the
  audit flagged ``_eviction_task`` as a private-attr reach-in
  surface that broke encapsulation.

The class is intentionally public (no leading underscore). The
old module-level ``_ManagedProcess`` name is aliased in ``shell.py``
for backwards compat with pickled state / test imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import signal
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.core.tools.process_supervisor import ProcessSupervisor

logger = logging.getLogger(__name__)

# Maximum output buffer size per process (1MB) — used to be
# in ``shell.py`` as ``_MAX_BUFFER``. Colocated with its only
# consumer (the reader task) now.
_MAX_BUFFER = 1_048_576


class ManagedProcess:
    """Tracks a running asyncio subprocess and its output.

    Construction wires the supervisor in — the reader task uses it
    to emit lines/completion events, and eviction routes through
    the supervisor's TTL config. Do NOT reach for a default
    supervisor: fresh test instances construct a fresh supervisor
    so isolation stays surgical.
    """

    #: Polymorphic discriminator paired with
    #: :attr:`OrphanProcess.is_orphan` (``True``). Backend code
    #: branches on ``mp.is_orphan`` rather than isinstance-checking
    #: a private name. Class attribute (not per-instance) so
    #: ``__slots__`` stays lean.
    is_orphan: bool = False

    __slots__ = (
        "proc",
        "output",
        "lock",
        "started_at",
        "cmd",
        "finished",
        "_read_cursor",
        "was_backgrounded",
        "_reader_task",
        "_log_file",
        "_supervisor",
    )

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        cmd: str,
        supervisor: ProcessSupervisor,
    ) -> None:
        self.proc = proc
        self.cmd = cmd
        self._supervisor = supervisor
        self.output: list[str] = []
        # File handle opened lazily on first backgrounded line —
        # foreground commands skip it (their output is consumed
        # immediately by the calling tool's reply). See
        # :meth:`_reader`. Explicit annotation so mypy doesn't infer
        # ``None`` and reject the later ``TextIOBase`` assignment.
        self._log_file: io.TextIOBase | None = None
        # Buffer is mutated by the reader task and by ``read``/``read_new``;
        # both run on the event loop so a regular lock would be enough,
        # but ``threading.Lock`` is also safe to call from
        # :meth:`ProcessSupervisor.cancel_foreground` (which runs on the
        # sync path) without any extra ceremony.
        self.lock = threading.Lock()
        self.started_at = time.monotonic()
        self.finished = False
        self._read_cursor: int = 0  # tracks position for read_new()
        self.was_backgrounded: bool = False
        # Reader task draining stdout into ``self.output``. Held so we
        # can ``await`` it on stop/finish to make sure trailing output
        # is captured before we report.
        # Eviction task handles live on
        # :class:`~ember_code.core.tools.process_registry.ProcessRegistry`
        # (keyed by pid) — this class no longer carries an
        # ``_eviction_task`` slot after the OOP audit closed the
        # private-attr reach-in.
        self._reader_task: asyncio.Task | None = None

    def start_reader(self) -> None:
        """Spawn the async reader task. Called once by the caller
        immediately after :meth:`ProcessRegistry.add`; separate
        from ``__init__`` so tests can construct a bare instance
        without a running loop."""
        self._reader_task = asyncio.create_task(self._reader())

    @property
    def reader_task(self) -> asyncio.Task | None:
        return self._reader_task

    async def wait_for_reader(self, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for the reader task to
        drain any trailing output after the process exits. Used by
        the foreground path to make sure the final tail is captured
        before we report to the LLM."""
        if self._reader_task is None:
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._reader_task, timeout=timeout)

    async def _reader(self) -> None:
        """Async task that drains stdout+stderr.

        When the read loop terminates (process exited and stdout
        closed) AND this process was backgrounded, fire registered
        completion subscribers so the agent gets notified that work
        it kicked off in the background has finished.
        """
        assert self.proc.stdout is not None
        try:
            while True:
                raw_line = await self.proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                with self.lock:
                    self.output.append(line)
                    # Trim if buffer is too large (keep last half)
                    total = sum(len(line) for line in self.output)
                    if total > _MAX_BUFFER:
                        self.output = self.output[len(self.output) // 2 :]
                # Fire line subscribers OUTSIDE the buffer lock —
                # they're independent (the FE watcher's loop hop
                # doesn't need the buffer to be consistent) and
                # nothing the subscriber can do should be allowed to
                # block other readers. ``was_backgrounded`` gates
                # the emit: foreground commands don't get a watcher
                # row, so streaming their lines would just be noise.
                if self.was_backgrounded:
                    self._supervisor.registry.emit_line(self.proc.pid, line)
                    self._tee_line_to_log(line)
        except (asyncio.CancelledError, ValueError):
            # CancelledError: stop_process cancelled us; the kill+wait
            # path takes over from here.
            # ValueError: stream closed mid-read.
            raise
        except Exception as exc:
            logger.warning("reader task for pid=%s errored: %s", self.proc.pid, exc)
        finally:
            # ``proc.returncode`` is only populated after
            # ``proc.wait()`` resolves. On some Linux/Python combos
            # the stdout close races ahead of that — without an
            # explicit wait, ``emit_completion`` would publish
            # ``exit_code=None``. The wait is cheap (the process
            # has already terminated by the time the reader loop
            # breaks) and bounded so a stuck child can't deadlock
            # the reader task.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            self.finished = True
            # Close the per-pid log file so the OS flushes and
            # the inode can be cleaned up by the eviction TTL.
            if self._log_file is not None:
                with contextlib.suppress(Exception):
                    self._log_file.close()
                self._log_file = None
            if self.was_backgrounded:
                self._supervisor.registry.emit_completion(self)

    def _tee_line_to_log(self, line: str) -> None:
        """Append ``line`` to the per-pid log file so an orphan
        (BE restart) can still read history. The log file is
        opened lazily on first line — keeps the foreground hot
        path free of file ops it doesn't need. Best-effort: a
        write failure is logged once and the file handle
        dropped (we'd rather lose log lines than block stdout
        drain)."""
        if self._log_file is None:
            self._log_file = self._supervisor.log_store.open(self.proc.pid)
        if self._log_file is None:
            return
        try:
            self._log_file.write(line + "\n")
        except OSError as exc:
            logger.debug(
                "process log write failed for pid=%s: %s",
                self.proc.pid,
                exc,
            )
            with contextlib.suppress(Exception):
                self._log_file.close()
            self._log_file = None

    def read(self, tail: int = 100) -> str:
        """Return the last ``tail`` lines of output."""
        with self.lock:
            lines = self.output[-tail:]
        return "\n".join(lines)

    def read_new(self, max_lines: int = 200) -> str:
        """Return only lines added since the last read_new() call."""
        with self.lock:
            new = self.output[self._read_cursor : self._read_cursor + max_lines]
            self._read_cursor = min(self._read_cursor + max_lines, len(self.output))
        return "\n".join(new)

    def is_running(self) -> bool:
        return self.proc.returncode is None

    def returncode(self) -> int | None:
        return self.proc.returncode

    def elapsed(self) -> float:
        """Seconds since the process started. Uses monotonic — safe
        against wall-clock jumps. Overrides the sibling
        :meth:`~ember_code.core.tools.orphan_process.OrphanProcess.elapsed`
        which uses epoch (orphans don't share our monotonic origin)."""
        return time.monotonic() - self.started_at

    def kill(self) -> None:
        """Kill the process tree.

        ``proc.kill()`` and ``os.killpg`` are both sync syscalls, so
        this is safe to call from the sync ``cancel_foreground`` path
        as well as from async code. Use ``await proc.wait()`` after
        if you need to confirm the process is reaped.
        """
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, OSError):
            self.proc.kill()
