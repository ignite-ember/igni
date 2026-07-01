"""MonitorManager — per-session lifecycle for plugin-declared
background monitors.

Each monitor runs as an :class:`asyncio.subprocess.Process` with
stdout+stderr merged into a rolling line buffer. The manager:

* Starts monitors at session bootstrap (``start_all``).
* Streams their output into the rolling buffer via a per-monitor
  drain task.
* Restarts crashed monitors according to ``restart`` policy with
  exponential backoff (capped at a small number of attempts so a
  permanently-broken monitor doesn't spin forever).
* Shuts everything down gracefully on session close
  (``shutdown_all``) — SIGTERM, then SIGKILL with a short
  deadline.

Why a separate manager (vs. reusing ``_ProcessRegistry``):
``_ProcessRegistry`` tracks agent-spawned shell processes whose
lifecycle is tied to a single shell tool call. Monitors are
session-scoped, plugin-owned, auto-restarted, and the agent
observes them through query tools rather than directly spawning
them. Different threat model → different manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

from ember_code.core.monitors.config import MonitorConfig

logger = logging.getLogger(__name__)


# How many recent output lines to keep per monitor. 1000 covers
# a typical "show me the last error" lookup without blowing
# memory on a chatty watcher. Old lines fall off the deque.
_OUTPUT_RING_SIZE = 1000

# Restart backoff schedule (seconds). After ``len(_BACKOFF)``
# consecutive crashes we stop restarting and mark the monitor
# ``failed`` — the agent / user can fix and call ``restart``.
_BACKOFF = (1.0, 2.0, 5.0, 15.0)

# How long to wait for SIGTERM to settle before SIGKILL. Two
# seconds is enough for a sane process to drain output and exit;
# anything that needs longer is misbehaving.
_TERM_GRACE = 2.0


class MonitorHandle:
    """One running monitor — a process plus its output buffer
    and lifecycle metadata. Held by ``MonitorManager``."""

    def __init__(self, config: MonitorConfig, project_dir: Path) -> None:
        self.config = config
        self._project_dir = project_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._output: deque[str] = deque(maxlen=_OUTPUT_RING_SIZE)
        self._drain_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._started_at: float = 0.0
        self._exit_code: int | None = None
        self._crash_count: int = 0
        self._status: str = "stopped"  # running | stopped | failed
        self._lock = asyncio.Lock()
        self._stopping: bool = False

    # ── Public state ─────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def status(self) -> str:
        return self._status

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def uptime_seconds(self) -> float:
        if self._status != "running" or self._started_at == 0.0:
            return 0.0
        return time.monotonic() - self._started_at

    def output_tail(self, lines: int = 40) -> list[str]:
        if lines <= 0:
            return []
        tail = list(self._output)[-lines:]
        return tail

    def snapshot(self) -> dict[str, Any]:
        """Status-line summary for ``MonitorManager.list_monitors``."""
        return {
            "name": self.name,
            "command": self.config.command,
            "status": self._status,
            "pid": self.pid,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "exit_code": self._exit_code,
            "crash_count": self._crash_count,
            "restart": self.config.restart,
        }

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the monitor and start draining its output.
        Idempotent — second call is a no-op when running."""
        async with self._lock:
            if self._status == "running":
                return
            await self._launch_locked()

    async def _launch_locked(self) -> None:
        cwd_str = self._resolve_cwd()
        env = self._resolve_env()
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
            self._status = "failed"
            self._output.append(f"[monitor failed to launch: {exc}]")
            logger.warning("Monitor %s launch failed: %s", self.name, exc)
            return
        self._started_at = time.monotonic()
        self._exit_code = None
        self._status = "running"
        self._drain_task = asyncio.create_task(
            self._drain_output(), name=f"monitor-drain-{self.name}"
        )

    def _resolve_cwd(self) -> str:
        if not self.config.cwd:
            return str(self._project_dir)
        cwd = Path(self.config.cwd)
        if not cwd.is_absolute():
            cwd = self._project_dir / cwd
        return str(cwd)

    def _resolve_env(self) -> dict[str, str]:
        # Inherit the parent environment, then layer the
        # monitor-specific overrides. Anything explicitly unset by
        # the manifest would need a sentinel; for now the simple
        # merge covers the documented use cases.
        import os

        env = dict(os.environ)
        env.update(self.config.env)
        return env

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
                self._status = "stopped"
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
            self._status = "stopped"
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


class MonitorManager:
    """Per-session manager — holds every plugin monitor."""

    def __init__(
        self,
        monitors: dict[str, MonitorConfig],
        project_dir: Path,
    ) -> None:
        self._configs = dict(monitors)
        self._project_dir = project_dir
        self._handles: dict[str, MonitorHandle] = {}
        self._supervisors: dict[str, asyncio.Task] = {}

    def list_names(self) -> list[str]:
        return sorted(self._configs.keys())

    def snapshot_all(self) -> list[dict[str, Any]]:
        """Status snapshot for every configured monitor (even ones
        we haven't started yet — they show ``status: "stopped"``)."""
        out: list[dict[str, Any]] = []
        for name in self.list_names():
            handle = self._handles.get(name)
            if handle is None:
                cfg = self._configs[name]
                out.append(
                    {
                        "name": name,
                        "command": cfg.command,
                        "status": "stopped",
                        "pid": None,
                        "uptime_seconds": 0.0,
                        "exit_code": None,
                        "crash_count": 0,
                        "restart": cfg.restart,
                    }
                )
            else:
                out.append(handle.snapshot())
        return out

    def output_tail(self, name: str, lines: int = 40) -> list[str]:
        handle = self._handles.get(name)
        if handle is None:
            return []
        return handle.output_tail(lines)

    async def start_all(self) -> None:
        """Launch every configured monitor + its supervisor.
        Idempotent — running monitors aren't restarted."""
        for name in self._configs:
            await self._start_one(name)

    async def _start_one(self, name: str) -> MonitorHandle:
        config = self._configs[name]
        handle = self._handles.get(name)
        if handle is None:
            handle = MonitorHandle(config, project_dir=self._project_dir)
            self._handles[name] = handle
        if handle.status != "running":
            await handle.start()
        if name not in self._supervisors or self._supervisors[name].done():
            self._supervisors[name] = asyncio.create_task(
                self._supervise(handle), name=f"monitor-supervisor-{name}"
            )
        return handle

    async def _supervise(self, handle: MonitorHandle) -> None:
        """Watch a monitor for exits; restart per policy with
        bounded exponential backoff."""
        try:
            while True:
                code = await handle.wait_exit()
                handle._exit_code = code
                if handle._stopping:
                    handle._status = "stopped"
                    return
                policy = handle.config.restart
                if policy == "never":
                    handle._status = "stopped"
                    return
                if policy == "on_crash" and (code == 0 or code is None):
                    handle._status = "stopped"
                    return
                # Bumped on every restart attempt; resets only on
                # explicit ``restart`` call (success-path
                # heuristic is hard to define for arbitrary
                # processes — we keep it simple).
                handle._crash_count += 1
                if handle._crash_count > len(_BACKOFF):
                    handle._status = "failed"
                    handle._output.append(
                        f"[monitor exceeded {len(_BACKOFF)} restart attempts — giving up]"
                    )
                    return
                delay = _BACKOFF[handle._crash_count - 1]
                handle._output.append(f"[monitor exited (code={code}); restarting in {delay:.0f}s]")
                await asyncio.sleep(delay)
                if handle._stopping:
                    return
                async with handle._lock:
                    await handle._launch_locked()
                if handle._status != "running":
                    return
        except asyncio.CancelledError:
            raise

    async def restart(self, name: str) -> str:
        """User-initiated restart — clears the crash counter and
        re-launches even a ``failed`` monitor."""
        if name not in self._configs:
            return f"Monitor not configured: {name!r}"
        await self.stop(name)
        handle = self._handles.get(name)
        if handle is not None:
            handle._crash_count = 0
        await self._start_one(name)
        return f"Restarted {name}."

    async def stop(self, name: str) -> str:
        if name not in self._configs:
            return f"Monitor not configured: {name!r}"
        sup = self._supervisors.pop(name, None)
        if sup is not None and not sup.done():
            sup.cancel()
            # CancelledError is BaseException, NOT Exception in
            # 3.8+ — catch both explicitly.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sup
        handle = self._handles.get(name)
        if handle is not None:
            await handle.stop()
        return f"Stopped {name}."

    async def shutdown_all(self) -> None:
        """Tear down every monitor + supervisor. Safe to call
        multiple times — already-stopped monitors are no-ops."""
        names = list(self._handles.keys())
        for name in names:
            await self.stop(name)
        self._handles.clear()
        self._supervisors.clear()
