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
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from agno.tools import Toolkit

logger = logging.getLogger(__name__)

# Maximum output buffer size per process (1MB)
_MAX_BUFFER = 1_048_576
# Maximum characters in a tool result returned to the AI
_MAX_RESULT_CHARS = 30_000


def _normalize_shell_args(args: list[str] | str) -> tuple[list[str], str]:
    """Accept any of the shapes the model tends to emit and return ``(argv, cmd_str)``.

    Models routinely send one of three shapes when asked to run a shell
    command. We normalize all three to a proper argv list (suitable for
    ``asyncio.create_subprocess_exec(*argv)``) plus a printable
    command string.

    Shapes handled:
      1. ``["ls", "-la"]`` — proper argv. Pass through.
      2. ``"ls -la"`` — single shell-style string. ``shlex.split`` it.
      3. ``["ls -la"]`` — single-element list whose only element is a
         whole shell command. Same as (2) — split it.
      4. ``["ls -la", "cat foo"]`` — list of shell commands to run in
         sequence. We join them with ``&&`` and run via ``sh -c``.
    """
    import shlex

    # String form
    if isinstance(args, str):
        argv = shlex.split(args)
        return argv, args

    # List form — at this point ``args`` is a list[str].
    if not args:
        return [], ""

    # Detect shape (3): ["ls -la"] — single element with whitespace.
    if len(args) == 1 and any(ch.isspace() for ch in args[0]):
        argv = shlex.split(args[0])
        return argv, args[0]

    # Detect shape (4): list of multiple commands, each containing
    # whitespace (means each element is its own shell command, not a
    # single argv token). Fall back to sh -c chained with ``&&``.
    multi_cmd = len(args) > 1 and all(any(ch.isspace() for ch in a) for a in args)
    if multi_cmd:
        joined = " && ".join(args)
        return ["sh", "-c", joined], joined

    # Default: treat as proper argv. ``["ls", "-la"]`` lands here.
    return list(args), " ".join(args)


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
    )

    def __init__(self, proc: asyncio.subprocess.Process, cmd: str):
        self.proc = proc
        self.cmd = cmd
        self.output: list[str] = []
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
            if self.was_backgrounded:
                _emit_completion(self)

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
        """Return (pid, cmd, elapsed_seconds) for running processes."""
        with self._lock:
            result = []
            for pid, mp in self._processes.items():
                if mp.is_running():
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

# Tracks the currently running foreground process so it can be killed on cancel.
_foreground_lock = threading.Lock()
_foreground_process: _ManagedProcess | None = None

# ── Background-process completion notifications ──────────────────────

_completion_subscribers_lock = threading.Lock()
_completion_subscribers: list[Any] = []  # list[Callable[[dict], None]]


def subscribe_to_process_completion(callback: Any) -> None:
    """Register ``callback(info)`` to be called when a backgrounded
    process exits. ``info`` is a dict with ``pid``, ``cmd``,
    ``exit_code``, ``duration_seconds``, ``output_tail`` (last ~40
    lines)."""
    with _completion_subscribers_lock:
        if callback not in _completion_subscribers:
            _completion_subscribers.append(callback)


def unsubscribe_from_process_completion(callback: Any) -> None:
    with _completion_subscribers_lock, contextlib.suppress(ValueError):
        _completion_subscribers.remove(callback)


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

    mp._eviction_task = asyncio.create_task(_evict())


def _emit_completion(mp: _ManagedProcess) -> None:
    """Notify subscribers that ``mp`` has exited.

    Called from the reader task once stdout closes. Runs on the event
    loop, but each subscriber is a plain callable — they're expected
    to be cheap and non-blocking (push onto a queue, schedule a
    coroutine via ``loop.call_soon_threadsafe``, etc.).
    """
    info = {
        "pid": mp.proc.pid,
        "cmd": mp.cmd,
        "exit_code": mp.proc.returncode,
        "duration_seconds": time.monotonic() - mp.started_at,
        "output_tail": mp.read(tail=40),
    }
    with _completion_subscribers_lock:
        subscribers = list(_completion_subscribers)
    for cb in subscribers:
        try:
            cb(info)
        except Exception as exc:  # don't let one bad subscriber kill the rest
            logger.warning("process completion subscriber raised: %s", exc)


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
            for name, func in self.functions.items():
                if name in confirm_tools:
                    func.requires_confirmation = True

    async def run_shell_command(
        self,
        args: list[str] | str,
        timeout: int = 7,
        background: bool = False,
        tail: int = 100,
    ) -> str:
        """Run a shell command and return its output.

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
            args: Either a proper argv list (``["ls", "-la"]``) OR a single
                shell-style string (``"ls -la"``) OR a list whose elements
                are themselves shell-style strings (``["ls -la", "cat foo"]``,
                run sequentially via ``sh -c``). The runtime normalizes
                whichever shape the model emits.
            timeout: Max seconds to wait for the command to finish. Default 7.
            background: If True, start in background and return PID immediately.
            tail: Number of output lines to return. Default 100.

        Returns:
            Command output, or a message with the PID for background processes.
        """
        argv, cmd_str = _normalize_shell_args(args)
        logger.info("Shell: running %s (timeout=%d, bg=%s)", cmd_str, timeout, background)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.base_dir) if self.base_dir else None,
                start_new_session=True,  # new process group for clean kills
            )
        except Exception as e:
            return f"Error starting command: {e}"

        mp = _ManagedProcess(proc, cmd_str)
        mp._reader_task = asyncio.create_task(mp._reader())
        pid = _registry.add(mp)

        if background:
            mp.was_backgrounded = True
            # Auto-watch for a few seconds to capture startup output or crash.
            # ``asyncio.sleep`` instead of ``time.sleep`` so the event loop
            # can keep servicing other work (HITL drain, FE stream) during
            # the wait — that was the headline reason for going async.
            await asyncio.sleep(3)
            output = mp.read_new()
            if not mp.is_running():
                rc = mp.returncode()
                _registry.remove(pid)
                return f"Background process exited immediately (code {rc}):\n{output}"
            status = f"Background process running (PID {pid}): {cmd_str}\n"
            if output:
                status += f"\nStartup output:\n{output}\n"
            else:
                status += "\nNo output yet (process is running silently).\n"
            status += f"\nUse watch_process({pid}) to monitor, stop_process({pid}) to stop."
            return status

        # Track as foreground so cancel_foreground() can kill it
        global _foreground_process
        with _foreground_lock:
            _foreground_process = mp

        # Wait for the process up to ``timeout`` seconds. ``wait_for``
        # raises ``TimeoutError`` if exceeded — that's the auto-
        # background path.
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True

        with _foreground_lock:
            _foreground_process = None

        if timed_out:
            mp.was_backgrounded = True
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
