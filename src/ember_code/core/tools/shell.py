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

Architecture (post-refactor):

* :class:`EmberShellTools` in this file is a thin ``Toolkit``
  subclass — the tool methods delegate to
  :class:`~ember_code.core.tools.process_supervisor.ProcessSupervisor`.
* :class:`~ember_code.core.tools.managed_process.ManagedProcess`
  owns per-process state (reader task, buffers, log file).
* :class:`~ember_code.core.tools.process_supervisor.ProcessSupervisor`
  owns cross-process state (registry, event bus, foreground slot,
  persistence store, TTL).

This module has ONE responsibility: the Agno toolkit. Callers that
need cross-process state (subscribing to the bus, poking the
registry, reaching for the singleton) should import directly from
:mod:`ember_code.core.tools.process_supervisor` /
:mod:`ember_code.core.tools.managed_process` /
:mod:`ember_code.core.tools.process_events`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from agno.tools import Toolkit

from ember_code.core.tools.managed_process import ManagedProcess
from ember_code.core.tools.process_supervisor import ProcessSupervisor
from ember_code.core.tools.process_supervisor_locator import supervisors
from ember_code.core.tools.shell_config import ShellToolsConfig
from ember_code.core.tools.tool_result import LLMResultBuffer

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        config: ShellToolsConfig | None = None,
        *,
        base_dir: str | Path | None = None,
        requires_confirmation_tools: list[str] | None = None,
        supervisor: ProcessSupervisor | None = None,
    ) -> None:
        """Build the toolkit.

        Prefer passing a fully-formed :class:`ShellToolsConfig`. The
        kwargs form (``base_dir=`` / ``requires_confirmation_tools=``)
        is kept so legacy call sites (registry.py, tests) work
        unchanged; they build a config internally.

        ``supervisor`` is injected for test isolation — pass a fresh
        :class:`ProcessSupervisor` and every tool call routes through
        it. In production, the constructor defaults to the process-
        wide supervisor via :attr:`supervisors` so all shell tool
        instances share one registry (one BE = one watcher panel).
        """
        if config is None:
            config = ShellToolsConfig(
                base_dir=Path(base_dir) if base_dir else None,
                requires_confirmation_tools=requires_confirmation_tools,
            )
        super().__init__(name=config.name)
        self._config = config
        self.base_dir: Path | None = config.base_dir
        self._supervisor = supervisor or supervisors.default()
        self._result_buffer = LLMResultBuffer()
        self.register(self.run_shell_command)
        self.register(self.read_process_output)
        self.register(self.watch_process)
        self.register(self.stop_process)
        self.register(self.list_processes)
        if config.requires_confirmation_tools:
            self._apply_confirmation(config.requires_confirmation_tools)

    def _apply_confirmation(self, names: list[str]) -> None:
        """Set ``requires_confirmation`` on every tool in ``names``.

        Async and sync tools live in separate Agno registries
        (``functions`` / ``async_functions``); walk both. A missing
        attribute is logged rather than swallowed so a future Agno
        rename surfaces loudly instead of silently skipping HITL
        confirmation.
        """
        self.requires_confirmation_tools = names
        for attr in ("functions", "async_functions"):
            registry = getattr(self, attr, None)
            if registry is None:
                logger.warning(
                    "EmberShellTools: Agno Toolkit has no ``%s`` attribute — "
                    "confirmation flags for %s will not be set. Agno base API "
                    "may have changed.",
                    attr,
                    names,
                )
                continue
            for name, func in registry.items():
                if name in names:
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

        mp = ManagedProcess(proc, command, self._supervisor)
        mp.start_reader()
        pid = self._supervisor.registry.add(mp)  # bare add — announce fires in run_*

        if background:
            return await self._supervisor.run_backgrounded(mp, pid, command)
        return await self._supervisor.run_foregrounded(mp, pid, timeout, tail)

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
        mp = self._supervisor.registry.get(pid)
        if mp is None:
            return f"No tracked process with PID {pid}."

        output = mp.read(tail=tail)
        if mp.is_running():
            elapsed = mp.elapsed()
            return self._result_buffer.truncate(
                f"[Running for {elapsed:.0f}s — PID {pid}]\n{output}"
            )

        # Process is finished — arm (or refresh) the eviction task.
        self._supervisor.registry.arm_eviction(mp)
        rc = mp.returncode()
        return self._result_buffer.truncate(f"[Finished — exit code {rc}]\n{output}")

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
        mp = self._supervisor.registry.get(pid)
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
        elapsed = mp.elapsed()

        if mp.is_running():
            if new_output:
                return f"[Running for {elapsed:.0f}s — PID {pid}]\nNew output:\n{new_output}"
            return (
                f"[Running for {elapsed:.0f}s — PID {pid}]\nNo new output in the last {seconds}s."
            )
        rc = mp.returncode()
        self._supervisor.registry.remove(pid)
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
        mp = self._supervisor.registry.get(pid)
        if mp is None:
            return f"No tracked process with PID {pid}."

        if not mp.is_running():
            rc = mp.returncode()
            output = mp.read(tail=20)
            self._supervisor.registry.remove(pid)
            return f"Process {pid} already finished (exit code {rc}).\nLast output:\n{output}"

        mp.kill()
        # Wait up to 5s for the process to actually exit.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(mp.proc.wait(), timeout=5.0)
        output = mp.read(tail=20)
        self._supervisor.registry.remove(pid)
        return f"Process {pid} stopped.\nLast output:\n{output}"

    async def list_processes(self) -> str:
        """List all running background processes.

        Returns:
            Table of running processes with PID, command, and elapsed time.
        """
        running = self._supervisor.registry.all_running()
        if not running:
            return "No background processes running."

        lines = ["PID    | Elapsed | Command", "-------+---------+--------"]
        for pid, cmd, elapsed in running:
            lines.append(f"{pid:<6} | {elapsed:>5.0f}s  | {cmd}")
        return "\n".join(lines)

    @staticmethod
    def cleanup() -> int:
        """Kill all tracked processes. Called on session shutdown."""
        return supervisors.default().registry.kill_all()
