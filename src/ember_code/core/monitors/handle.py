"""MonitorHandle — one running monitor process plus its output
buffer and lifecycle metadata.

The handle owns *every* status transition. External code (the
supervisor, the manager, tests) must go through the public
methods below — no more reach-in writes of ``_status``,
``_crash_count``, ``_exit_code``, ``_stopping``, ``_output``, or
``_lock``. Status transitions are gated by dedicated methods so
the invariant "status only ever moves through
:class:`MonitorStatus`" holds without scattered string writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from pathlib import Path

from ember_code.core.monitors.config import MonitorConfig
from ember_code.core.monitors.models import MonitorSnapshot, MonitorStatus

logger = logging.getLogger(__name__)


# How many recent output lines to keep per monitor. 1000 covers
# a typical "show me the last error" lookup without blowing
# memory on a chatty watcher. Old lines fall off the deque.
_OUTPUT_RING_SIZE = 1000

# How long to wait for SIGTERM to settle before SIGKILL. Two
# seconds is enough for a sane process to drain output and exit;
# anything that needs longer is misbehaving.
_TERM_GRACE = 2.0


class MonitorHandle:
    """One running monitor — a process plus its output buffer
    and lifecycle metadata. Held by :class:`MonitorManager`.

    All state fields are single-underscore *private-by-convention*.
    Anything the supervisor / manager / test needs goes through a
    public method or property below.
    """

    def __init__(self, config: MonitorConfig, project_dir: Path) -> None:
        self.config = config
        self._project_dir = project_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._output: deque[str] = deque(maxlen=_OUTPUT_RING_SIZE)
        self._drain_task: asyncio.Task | None = None
        self._started_at: float = 0.0
        self._exit_code: int | None = None
        self._crash_count: int = 0
        self._status: MonitorStatus = MonitorStatus.STOPPED
        self._lock = asyncio.Lock()
        self._stopping: bool = False

    # ── Public state ─────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def status(self) -> MonitorStatus:
        return self._status

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def crash_count(self) -> int:
        return self._crash_count

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def uptime_seconds(self) -> float:
        if self._status is not MonitorStatus.RUNNING or self._started_at == 0.0:
            return 0.0
        return time.monotonic() - self._started_at

    def is_stopping(self) -> bool:
        """True once :meth:`stop` has been entered but before the
        underlying process is fully torn down. Read by the
        supervisor to skip restarts on deliberate stops."""
        return self._stopping

    def output_tail(self, lines: int = 40) -> list[str]:
        if lines <= 0:
            return []
        return list(self._output)[-lines:]

    def append_output(self, line: str) -> None:
        """Append an operator/supervisor annotation to the rolling
        buffer (used for messages like "restarting in 5s")."""
        self._output.append(line)

    def snapshot(self) -> MonitorSnapshot:
        """Status-line summary for
        :meth:`MonitorManager.snapshot_all`."""
        return MonitorSnapshot(
            name=self.name,
            command=self.config.command,
            status=self._status,
            pid=self.pid,
            uptime_seconds=round(self.uptime_seconds, 2),
            exit_code=self._exit_code,
            crash_count=self._crash_count,
            restart=self.config.restart,
        )

    # ── Method-gated status transitions ──────────────────────

    def mark_running(self) -> None:
        """Called after a successful subprocess launch. Reads the
        pid off ``self._proc`` internally — the invariant
        "pid is whatever the subprocess has" belongs entirely to
        the handle."""
        self._started_at = time.monotonic()
        self._exit_code = None
        self._status = MonitorStatus.RUNNING

    def mark_stopped(self, exit_code: int | None = None) -> None:
        if exit_code is not None:
            self._exit_code = exit_code
        self._status = MonitorStatus.STOPPED

    def mark_failed(self, reason: str) -> None:
        """Move to ``failed`` and record the reason in the output
        ring (that's how the agent surfaces "why is this red")."""
        self._status = MonitorStatus.FAILED
        self._output.append(reason)

    def reset_crash_count(self) -> None:
        """Called by :meth:`MonitorManager.restart` — the explicit-
        user path forgets the crash count."""
        self._crash_count = 0

    def bump_crash_count(self) -> int:
        """Increment the crash counter and return the new value.
        Used by the supervisor's backoff logic."""
        self._crash_count += 1
        return self._crash_count

    def bump_crash_count_to(self, count: int) -> None:
        """Seed the crash counter — for tests that want to verify
        the counter is cleared without racing an actual crash
        loop. Public and honestly named; not a backdoor."""
        self._crash_count = count

    def note_exit(self, exit_code: int | None) -> None:
        """Record the exit code observed by the supervisor without
        moving the status field — the supervisor may still choose
        to restart. Status motion is a separate call."""
        self._exit_code = exit_code

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the monitor and start draining its output.
        Idempotent — second call is a no-op when running."""
        async with self._lock:
            if self._status is MonitorStatus.RUNNING:
                return
            await self._launch_locked()

    async def relaunch_under_lock(self) -> None:
        """Public entry point the supervisor calls when it decides
        to relaunch a crashed monitor. Acquires the handle lock
        internally — the supervisor never touches ``self._lock``
        directly."""
        async with self._lock:
            await self._launch_locked()

    async def _launch_locked(self) -> None:
        cwd_str = self.config.resolve_cwd(self._project_dir)
        env = self.config.resolve_env()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.config.command,
                *self.config.args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd_str,
                env=env,
            )
        except (OSError, FileNotFoundError) as exc:
            self.mark_failed(f"[monitor failed to launch: {exc}]")
            logger.warning("Monitor %s launch failed: %s", self.name, exc)
            return
        self.mark_running()
        self._drain_task = asyncio.create_task(
            self._drain_output(), name=f"monitor-drain-{self.name}"
        )

    async def _drain_output(self) -> None:
        """Pump the merged stdout+stderr stream into the rolling
        buffer. Exits when the stream closes (process gone)."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                self._output.append(line.decode("utf-8", errors="replace").rstrip("\n"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Monitor %s drain crashed: %s", self.name, exc)

    async def stop(self) -> None:
        """Graceful SIGTERM → wait → SIGKILL if it doesn't exit."""
        async with self._lock:
            self._stopping = True
            if self._proc is None or self._proc.returncode is not None:
                self._status = MonitorStatus.STOPPED
                self._stopping = False
                return
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                self._exit_code = await asyncio.wait_for(self._proc.wait(), timeout=_TERM_GRACE)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
                with contextlib.suppress(Exception):
                    self._exit_code = await asyncio.wait_for(self._proc.wait(), timeout=1.0)
            self._status = MonitorStatus.STOPPED
            self._stopping = False
            if self._drain_task and not self._drain_task.done():
                self._drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._drain_task

    async def wait_exit(self) -> int | None:
        """Block until the underlying process exits, then return
        the exit code. Used by the supervisor loop to detect
        crashes."""
        if self._proc is None:
            return None
        try:
            return await self._proc.wait()
        except asyncio.CancelledError:
            raise


__all__ = ["MonitorHandle"]
