"""Non-blocking shell tool with process management.

Replaces Agno's ShellTools with an async-aware implementation that:
- Runs commands with a configurable timeout (default 7s)
- Supports background/long-running processes (servers, watchers)
- Lets the AI read output incrementally and stop processes
- Kills subprocesses on cancellation instead of hanging forever

The public tool methods are ``async def`` so Agno's async tool
dispatcher (``Function.aexecute``) can ``await`` them. An earlier
sync implementation looked correct but actually blocked the event
loop for up to ``timeout`` seconds (and a hard 3s on every
``background=True`` call) — the loop sat there frozen, the HITL
multiplexer drain stalled, FE messages stopped flowing. Pure async
fixes that: every wait is cooperative.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agno.tools import Toolkit

from ember_code.core.tools import process_log
from ember_code.core.tools.process_bus import ProcessEventBus
from ember_code.core.tools.process_store import (
    BackgroundProcessRow,
    BackgroundProcessStore,
    now_epoch,
)

# Backwards-compat re-exports — the orphan cluster moved to
# ``shell_orphan.py`` in iter 189, but tests + ``server_processes``
# still reach for ``_OrphanProcess`` / ``rehydrate_orphan_processes``
# via the ``shell`` module.
from ember_code.core.tools.shell_orphan import (  # noqa: F401
    _OrphanProcess,
    _OrphanProcStub,
    rehydrate_orphan_processes,
)

logger = logging.getLogger(__name__)

# Maximum output buffer size per process (1MB)
_MAX_BUFFER = 1_048_576
# Maximum characters in a tool result returned to the AI
_MAX_RESULT_CHARS = 30_000


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    """Truncate output to avoid sending huge tool results to the LLM."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half] + f"\n\n... ({len(text) - limit} characters truncated) ...\n\n" + text[-half:]
    )


class _ManagedProcess:
    """Tracks a running asyncio subprocess and its output."""

    __slots__ = (
        "proc",
        "output",
        "lock",
        "started_at",
        "cmd",
        "finished",
        "_read_cursor",
        "was_backgrounded",
        "_eviction_task",
        "_reader_task",
        "_log_file",
    )

    def __init__(self, proc: asyncio.subprocess.Process, cmd: str):
        self.proc = proc
        self.cmd = cmd
        self.output: list[str] = []
        # File handle opened lazily on first backgrounded line —
        # foreground commands skip it (their output is consumed
        # immediately by the calling tool's reply). See
        # ``_reader``. Explicit annotation so mypy doesn't infer
        # ``None`` and reject the later ``TextIOBase`` assignment
        # in ``_reader`` when the file is opened.
        self._log_file: io.TextIOBase | None = None
        # Buffer is mutated by the reader task and by ``read``/``read_new``;
        # both run on the event loop so a regular lock would be enough,
        # but ``threading.Lock`` is also safe to call from
        # ``cancel_foreground`` (which runs on the sync path) without
        # any extra ceremony.
        self.lock = threading.Lock()
        self.started_at = time.monotonic()
        self.finished = False
        self._read_cursor: int = 0  # tracks position for read_new()
        self.was_backgrounded: bool = False
        # Eviction task armed by ``read_process_output`` on the first
        # post-completion read. Each subsequent read cancels and
        # re-arms it, giving us "10 min after the most recent read".
        self._eviction_task: asyncio.Task | None = None
        # Reader task draining stdout into ``self.output``. Held so we
        # can ``await`` it on stop/finish to make sure trailing output
        # is captured before we report.
        self._reader_task: asyncio.Task | None = None

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
                    _emit_line(self.proc.pid, line)
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
            # explicit wait, ``_emit_completion`` would publish
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
                _emit_completion(self)

    def _tee_line_to_log(self, line: str) -> None:
        """Append ``line`` to the per-pid log file so an orphan
        (BE restart) can still read history. The log file is
        opened lazily on first line — keeps the foreground hot
        path free of file ops it doesn't need. Best-effort: a
        write failure is logged once and the file handle
        dropped (we'd rather lose log lines than block stdout
        drain)."""
        if self._log_file is None:
            self._log_file = process_log.open_log(
                self.proc.pid, process_log.get_default_project_dir()
            )
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
        """Return the last `tail` lines of output."""
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


class _ProcessRegistry:
    """Global registry of managed background processes.

    Stays sync (``threading.Lock`` + dict). All operations are O(1)
    and don't await; using a sync lock means the registry is callable
    from both async tools and the sync ``cancel_foreground`` path.
    """

    def __init__(self) -> None:
        self._processes: dict[int, _ManagedProcess] = {}
        self._lock = threading.Lock()
        # Persistence store lives on the registry rather than as a
        # bare module-level global (AP6). ``None`` until
        # ``set_persistence_store`` is called from
        # ``BackendServer.startup``; persistence helpers no-op
        # silently until then so headless / test paths that don't
        # boot the DB stay uncoupled.
        self._persistence_store: Any | None = None

    def set_persistence_store(self, store: Any | None) -> None:
        """Wire this registry to a :class:`BackgroundProcessStore`
        instance. Pass ``None`` to clear (test isolation)."""
        self._persistence_store = store

    def _persist_add(self, pid: int, cmd: str) -> None:
        """Fire-and-forget upsert of a freshly-registered process.
        Called from ``_reader`` on the sync spawn path, so we
        schedule the async write on the running loop and don't
        await — losing the write to a freak race is preferable to
        blocking spawn on a DB roundtrip.

        A queued task that hasn't run yet when the BE exits leaks
        the row in memory but never hits disk; the next BE startup
        will simply not see that process. That's fine — we couldn't
        track it either way."""
        store = self._persistence_store
        if store is None:
            return
        try:
            # ``get_running_loop`` raises RuntimeError if there's no
            # running loop. ``get_event_loop`` is deprecated and,
            # crucially on 3.11/3.12, silently CREATES a new loop
            # when none is running — the ``ensure_future`` below
            # schedules onto that phantom loop and the coroutine
            # never runs, leaking DB locks and hanging pytest
            # sessions for the full 6-hour job timeout.
            asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            pgid = None
        row = BackgroundProcessRow(pid=pid, cmd=cmd, pgid=pgid, started_at=now_epoch())
        asyncio.ensure_future(store.upsert(row))

    def _persist_remove(self, pid: int) -> None:
        """Fire-and-forget delete. Same semantics as ``_persist_add``."""
        store = self._persistence_store
        if store is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(store.remove(pid))

    def add(self, mp: _ManagedProcess) -> int:
        pid = mp.proc.pid
        with self._lock:
            self._processes[pid] = mp
        return pid

    def get(self, pid: int) -> _ManagedProcess | None:
        with self._lock:
            return self._processes.get(pid)

    def remove(self, pid: int) -> None:
        with self._lock:
            self._processes.pop(pid, None)

    def all_running(self) -> list[tuple[int, str, float]]:
        """Return (pid, cmd, elapsed_seconds) for running processes.

        Orphans (rehydrated from a previous BE) store epoch
        seconds in ``_started_epoch`` instead of monotonic in
        ``started_at`` — the previous monotonic value belongs to
        a process that no longer exists. Branch on the attribute
        so both kinds report correct elapsed time.
        """
        with self._lock:
            result = []
            for pid, mp in self._processes.items():
                if mp.is_running():
                    if hasattr(mp, "_started_epoch"):
                        elapsed = time.time() - mp._started_epoch  # type: ignore[attr-defined]
                    else:
                        elapsed = time.monotonic() - mp.started_at
                    result.append((pid, mp.cmd, elapsed))
            return result

    def kill_all(self) -> int:
        """Kill all tracked processes synchronously. Returns count killed.

        Used at BE shutdown — we just send SIGKILL and don't wait for
        the processes to fully reap. The async event loop is already
        torn down by this point, so we can't ``await proc.wait()``.
        """
        with self._lock:
            count = 0
            for mp in self._processes.values():
                if mp.is_running():
                    mp.kill()
                    count += 1
            self._processes.clear()
            return count


# Singleton registry shared across all tool instances
_registry = _ProcessRegistry()


# ── Cross-restart persistence (orphan tracking) ─────────────────────
#
# A BE restart drops the in-memory registry, but processes spawned
# with ``start_new_session=True`` keep running. Without the DB-
# backed store below, those orphans become invisible — port 3000
# is still held but the watcher reports "0 processes".
#
# Persistence state lives on ``_registry`` (the singleton
# :class:`_ProcessRegistry` further down) — see
# ``_ProcessRegistry._persistence_store`` and its
# ``set_persistence_store`` /  ``_persist_add`` / ``_persist_remove``
# methods. The three module-level wrappers below stay as thin
# delegates so historical import sites
# (``from shell import set_process_store, _persist_remove``,
# tests, ``shell_orphan.py``) keep working unchanged.


def set_process_store(store: Any | None) -> None:
    """Wire the registry to a :class:`BackgroundProcessStore`
    instance. Called from ``BackendServer.startup`` with the
    boot session's project-scoped store. Pass ``None`` to clear
    (test isolation)."""
    _registry.set_persistence_store(store)


def _persist_add(pid: int, cmd: str) -> None:
    """Module-level delegate — see
    :meth:`_ProcessRegistry._persist_add`."""
    _registry._persist_add(pid, cmd)


def _persist_remove(pid: int) -> None:
    """Module-level delegate — see
    :meth:`_ProcessRegistry._persist_remove`."""
    _registry._persist_remove(pid)


# Tracks the currently running foreground process so it can be killed on cancel.
_foreground_lock = threading.Lock()
_foreground_process: _ManagedProcess | None = None

# ── Background-process lifecycle event bus ──────────────────────────
#
# Before the refactor this module had NINE names for what is
# fundamentally one concept (three parallel subscribe/unsubscribe
# APIs + three lists + three locks). :class:`ProcessEventBus`
# collapses that into one object with ``on(event, cb)`` /
# ``off(event, cb)`` / ``emit(event, payload)``. See
# ``core/tools/process_bus.py`` for the class + docstring.
#
# The module-level ``subscribe_to_*`` / ``unsubscribe_from_*``
# functions below are thin backwards-compat wrappers around the
# bus — every existing caller continues to work unchanged, but
# new callers should use ``event_bus.on(...)`` directly.

_event_bus = ProcessEventBus()


def subscribe_to_process_completion(callback: Any) -> None:
    """Legacy wrapper — see :meth:`ProcessEventBus.on` and the
    class docstring for the event payload shape."""
    _event_bus.on("exit", callback)


def unsubscribe_from_process_completion(callback: Any) -> None:
    _event_bus.off("exit", callback)


def subscribe_to_process_line(callback: Any) -> None:
    """Legacy wrapper — payload ``{pid, line}``. Called per stdout/
    stderr line. Subscribers must be cheap and non-blocking (the
    reader task fires them synchronously on the event loop)."""
    _event_bus.on("line", callback)


def unsubscribe_from_process_line(callback: Any) -> None:
    _event_bus.off("line", callback)


def subscribe_to_process_start(callback: Any) -> None:
    """Legacy wrapper — payload ``{pid, cmd, started_at}``. Fired
    once when a backgrounded process is registered."""
    _event_bus.on("start", callback)


def unsubscribe_from_process_start(callback: Any) -> None:
    _event_bus.off("start", callback)


def _emit_start(mp: _ManagedProcess) -> None:
    """Fire start subscribers via the bus. Also persists the row
    to the project's state.db so the watcher can rehydrate it
    across BE restarts (see :func:`rehydrate_orphan_processes`)."""
    # Persist FIRST so a subscriber that crashes can't sink the
    # restart recovery (subscribers are FE pushes; the DB row is
    # load-bearing for orphan tracking).
    _persist_add(mp.proc.pid, mp.cmd)
    _event_bus.emit(
        "start",
        {
            "pid": mp.proc.pid,
            "cmd": mp.cmd,
            "started_at": time.time(),
        },
    )


def _emit_line(pid: int, line: str) -> None:
    """Fire line subscribers via the bus. Called by the reader
    task per line. Hot path — the bus already snapshots the
    subscriber list under a lock, so this stays contention-free
    at the callsite."""
    _event_bus.emit("line", {"pid": pid, "line": line})


# Default eviction delay (seconds) — 10 minutes after the most recent
# read of a finished process. Module-level so tests can monkeypatch.
_FINISHED_PROCESS_TTL_SECONDS = 600


def _arm_eviction_task(mp: _ManagedProcess, pid: int) -> None:
    """Arm/refresh the async eviction task for a finished process.

    Called from ``read_process_output`` whenever the agent reads a
    finished process. Cancels any existing task first so the TTL
    resets — i.e. "10 min after the *most recent* read", not 10 min
    after the first.
    """
    if mp._eviction_task is not None and not mp._eviction_task.done():
        mp._eviction_task.cancel()

    async def _evict() -> None:
        try:
            await asyncio.sleep(_FINISHED_PROCESS_TTL_SECONDS)
        except asyncio.CancelledError:
            return
        if _registry.get(pid) is mp:
            _registry.remove(pid)
            logger.debug("evicted process pid=%d after TTL", pid)
            # Per-pid log file follows the same TTL — the agent's
            # in-memory buffer and the on-disk tail-able copy
            # should go away together. Skipped if the registry
            # row was already swapped (pid reuse edge case).
            process_log.cleanup(pid, process_log.get_default_project_dir())

    mp._eviction_task = asyncio.create_task(_evict())


def _emit_completion(mp: _ManagedProcess) -> None:
    """Notify subscribers via the bus that ``mp`` has exited.

    Called from the reader task once stdout closes. Runs on the event
    loop, but each subscriber is a plain callable — they're expected
    to be cheap and non-blocking (push onto a queue, schedule a
    coroutine via ``loop.call_soon_threadsafe``, etc.).
    """
    # Prune the persisted row before the FE-facing subscribers
    # fire. Otherwise a BE restart between exit and the FE seeing
    # the ``process_exited`` push would leave a dead pid in the
    # store, surfacing as an orphan that's already gone (the
    # liveness probe would catch it but a stale row is a smell
    # we can avoid).
    _persist_remove(mp.proc.pid)
    _event_bus.emit(
        "exit",
        {
            "pid": mp.proc.pid,
            "cmd": mp.cmd,
            "exit_code": mp.proc.returncode,
            "duration_seconds": time.monotonic() - mp.started_at,
            "output_tail": mp.read(tail=40),
        },
    )


def cancel_foreground() -> bool:
    """Kill the active foreground process. Called on Escape/cancel.

    Stays sync because the cancel path (``BackendServer.cancel_run``)
    is sync. ``proc.kill()`` itself is a sync syscall, so this works
    even though the process is owned by an async task.

    Returns True if a process was killed.
    """
    global _foreground_process
    with _foreground_lock:
        mp = _foreground_process
        if mp is not None and mp.is_running():
            mp.kill()
            _foreground_process = None
            return True
    return False


async def _run_backgrounded(
    mp: _ManagedProcess, pid: int, command: str
) -> str:
    """Handle the ``background=True`` path of ``run_shell_command``.

    Auto-watches for a few seconds after spawn to capture startup
    output or an early crash. ``asyncio.sleep`` (not ``time.sleep``)
    so the event loop keeps servicing other work (HITL drain, FE
    stream) during the wait — the headline reason for going async.

    Distinguishes a clean fast completion (ran to completion inside
    the 3 s grace window) from a startup crash. The LLM consumes
    this string — calling a successful run "exited immediately"
    tends to nudge the model into a needless retry.
    """
    mp.was_backgrounded = True
    _emit_start(mp)
    await asyncio.sleep(3)
    output = mp.read_new()
    if not mp.is_running():
        rc = mp.returncode()
        _registry.remove(pid)
        if rc != 0:
            return f"Background process exited with code {rc}:\n{output}"
        return f"Background process finished during startup window (code 0):\n{output}"
    status = f"Background process running (PID {pid}): {command}\n"
    if output:
        status += f"\nStartup output:\n{output}\n"
    else:
        status += "\nNo output yet (process is running silently).\n"
    status += f"\nUse watch_process({pid}) to monitor, stop_process({pid}) to stop."
    return status


async def _run_foregrounded(
    mp: _ManagedProcess,
    proc: asyncio.subprocess.Process,
    pid: int,
    timeout: int,
    tail: int,
) -> str:
    """Handle the ``background=False`` path of ``run_shell_command``.

    Waits up to ``timeout`` seconds for the process to complete.
    On timeout, promotes it to a background process (auto-
    background) and returns a "still running" description. On
    normal completion, waits briefly for the reader task to
    capture trailing output, then removes the registry entry
    and returns the tail.
    """
    global _foreground_process
    with _foreground_lock:
        _foreground_process = mp

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True

    with _foreground_lock:
        _foreground_process = None

    if timed_out:
        mp.was_backgrounded = True
        _emit_start(mp)
        output = mp.read(tail=tail)
        return _truncate(
            f"Command still running after {timeout}s — backgrounded as PID {pid}.\n"
            f"Use read_process_output({pid}) to check output.\n"
            f"Use stop_process({pid}) to stop it.\n\n"
            f"Output so far:\n{output}"
        )

    # Command finished — wait briefly for the reader task to
    # capture any trailing output buffered after proc.wait().
    if mp._reader_task is not None:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(mp._reader_task, timeout=2.0)
    output = mp.read(tail=tail)
    rc = proc.returncode
    _registry.remove(pid)

    if rc != 0:
        return _truncate(f"Command exited with code {rc}:\n{output}")
    return _truncate(output)


class EmberShellTools(Toolkit):
    """Non-blocking async shell tool with process management.

    Provides five tools (all ``async def`` so they don't block Agno's
    event loop):
    - run_shell_command: Execute a command (waits up to timeout, then backgrounds)
    - read_process_output: Read output from a backgrounded process (idempotent)
    - watch_process: Watch a process for new output for a window
    - stop_process: Stop a running process
    - list_processes: List running background processes
    """

    def __init__(self, base_dir: str | None = None, **kwargs):
        # Extract requires_confirmation_tools before super().__init__
        # because Agno validates it before register() is called.
        confirm_tools = kwargs.pop("requires_confirmation_tools", None)
        super().__init__(name="ember_shell", **kwargs)
        self.base_dir = Path(base_dir) if base_dir else None
        self.register(self.run_shell_command)
        self.register(self.read_process_output)
        self.register(self.watch_process)
        self.register(self.stop_process)
        self.register(self.list_processes)
        if confirm_tools:
            self.requires_confirmation_tools = confirm_tools
            # Iterate BOTH ``functions`` and ``async_functions``.
            # Agno's ``Toolkit.register`` routes async callables
            # (e.g. ``run_shell_command``) into ``async_functions``
            # and sync ones into ``functions``. The previous code
            # only touched ``functions``, so ``requires_confirmation``
            # was never set on the shell tool — Agno never paused,
            # no HITL dialog fired, and the model got the raw
            # tool_hook "no canUseTool bridge" string back. Look
            # in git blame for the incident where fresh installs
            # couldn't run a background shell command after
            # upgrading.
            for registry in (self.functions, self.async_functions):
                for name, func in registry.items():
                    if name in confirm_tools:
                        func.requires_confirmation = True

    async def run_shell_command(
        self,
        command: str,
        timeout: int = 7,
        background: bool = False,
        tail: int = 100,
    ) -> str:
        """Run a shell command and return its output.

        Pass ONE shell command string — exactly as you would type it at
        a terminal. The string is executed via ``/bin/sh -c``, so full
        shell syntax works: pipes ``|``, redirection ``>`` / ``2>&1``,
        chaining ``&&`` / ``||`` / ``;``, variable expansion ``$VAR``,
        env-var prefixes (``PATH=X cmd``), command substitution
        ``$(...)``, globs, and builtins like ``cd`` / ``export``.

        DO NOT pass an argv list — pass a single string.
            Good: ``"ls -la | wc -l"``
            Good: ``"cd portal && npm run build"``
            Bad:  ``["ls", "-la"]``

        For short-lived commands (ls, git, grep, cat, curl), waits up to
        `timeout` seconds and returns the output.

        For long-running commands (servers, watchers, anything that runs
        indefinitely), you MUST set background=True. This starts the process
        and returns its PID with initial output. Use watch_process(pid) to
        monitor and stop_process(pid) to stop.

        Examples of commands that MUST use background=True:
        - uvicorn, gunicorn, flask run, npm start, python -m http.server
        - docker compose up, npm run dev, tail -f, watch
        - Any command that starts a server or runs indefinitely

        If a foreground command exceeds the timeout, it is automatically
        backgrounded and its PID is returned.

        Args:
            command: A single shell command string.
            timeout: Max seconds to wait for the command to finish. Default 7.
            background: If True, start in background and return PID immediately.
            tail: Number of output lines to return. Default 100.

        Returns:
            Command output, or a message with the PID for background processes.
        """
        if isinstance(command, list):
            command = " ".join(command)
        logger.info("Shell: running %s (timeout=%d, bg=%s)", command, timeout, background)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.base_dir) if self.base_dir else None,
                start_new_session=True,  # new process group for clean kills
            )
        except Exception as e:
            return f"Error starting command: {e}"

        mp = _ManagedProcess(proc, command)
        mp._reader_task = asyncio.create_task(mp._reader())
        pid = _registry.add(mp)

        if background:
            return await _run_backgrounded(mp, pid, command)
        return await _run_foregrounded(mp, proc, pid, timeout, tail)

    async def read_process_output(self, pid: int, tail: int = 100) -> str:
        """Read recent output from a running or finished background process.

        The agent can call this repeatedly — both before and after the
        process has finished — and pass different ``tail`` values
        (e.g. ``tail=50`` to peek, then ``tail=500`` to dig deeper if
        the peek looked off). The buffer is in-memory, capped at
        ~1MB per process. After the first read of a finished
        process, an eviction task (default 10 min) is armed; each
        subsequent read resets it, so as long as the agent is
        actively engaging with the output the entry sticks around.
        Use ``stop_process(pid)`` to free it explicitly while it's
        still running.

        Args:
            pid: Process ID returned by run_shell_command.
            tail: Number of lines to return. Default 100.

        Returns:
            Recent output lines and process status.
        """
        mp = _registry.get(pid)
        if mp is None:
            return f"No tracked process with PID {pid}."

        output = mp.read(tail=tail)
        if mp.is_running():
            elapsed = time.monotonic() - mp.started_at
            return _truncate(f"[Running for {elapsed:.0f}s — PID {pid}]\n{output}")

        # Process is finished — arm (or refresh) the eviction task.
        _arm_eviction_task(mp, pid)
        rc = mp.returncode()
        return _truncate(f"[Finished — exit code {rc}]\n{output}")

    async def watch_process(self, pid: int, seconds: int = 10) -> str:
        """Watch a background process for a period, then return new output.

        Collects output for `seconds` seconds (or until the process exits),
        then returns only the NEW lines produced during that window. Use this
        after starting a background process to verify it works, or to monitor
        a running server for errors. Call repeatedly to keep watching.

        Args:
            pid: Process ID to watch.
            seconds: How many seconds to watch (1–30). Default 10.

        Returns:
            New output produced during the watch window, plus process status.
        """
        mp = _registry.get(pid)
        if mp is None:
            return f"No tracked process with PID {pid}."

        seconds = max(1, min(seconds, 30))

        # Wait for output or process exit. ``asyncio.wait_for`` on
        # ``proc.wait()`` is the cleanest way — if the process exits
        # before the timeout, we return early; otherwise we sleep
        # exactly ``seconds`` seconds.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(mp.proc.wait(), timeout=seconds)

        new_output = mp.read_new()
        elapsed = time.monotonic() - mp.started_at

        if mp.is_running():
            if new_output:
                return f"[Running for {elapsed:.0f}s — PID {pid}]\nNew output:\n{new_output}"
            return (
                f"[Running for {elapsed:.0f}s — PID {pid}]\nNo new output in the last {seconds}s."
            )
        rc = mp.returncode()
        _registry.remove(pid)
        if new_output:
            return f"[Exited with code {rc} after {elapsed:.0f}s]\nOutput:\n{new_output}"
        return f"[Exited with code {rc} after {elapsed:.0f}s]\nNo new output before exit."

    async def stop_process(self, pid: int) -> str:
        """Stop a running background process.

        Args:
            pid: Process ID to stop.

        Returns:
            Confirmation message.
        """
        mp = _registry.get(pid)
        if mp is None:
            return f"No tracked process with PID {pid}."

        if not mp.is_running():
            rc = mp.returncode()
            output = mp.read(tail=20)
            _registry.remove(pid)
            return f"Process {pid} already finished (exit code {rc}).\nLast output:\n{output}"

        mp.kill()
        # Wait up to 5s for the process to actually exit.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(mp.proc.wait(), timeout=5.0)
        output = mp.read(tail=20)
        _registry.remove(pid)
        return f"Process {pid} stopped.\nLast output:\n{output}"

    async def list_processes(self) -> str:
        """List all running background processes.

        Returns:
            Table of running processes with PID, command, and elapsed time.
        """
        running = _registry.all_running()
        if not running:
            return "No background processes running."

        lines = ["PID    | Elapsed | Command", "-------+---------+--------"]
        for pid, cmd, elapsed in running:
            lines.append(f"{pid:<6} | {elapsed:>5.0f}s  | {cmd}")
        return "\n".join(lines)

    @staticmethod
    def cleanup() -> int:
        """Kill all tracked processes. Called on session shutdown."""
        return _registry.kill_all()
