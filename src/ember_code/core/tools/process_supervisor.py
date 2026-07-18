"""ProcessSupervisor — thin composition root for background shells.

Post-split (see the refactor audit): this file used to fold nine
concerns into one class + a module-level singleton slot. The
concerns now live in four collaborating classes:

* :class:`~ember_code.core.tools.process_registry.ProcessRegistry`
  — pid → mp map, event bus, log store, persistence store, TTL,
  eviction tasks, ``add`` / ``announce_start`` / ``remove`` /
  ``emit_completion`` coordination (this is where the
  behaviour + data audit-flag moved).
* :class:`~ember_code.core.tools.tool_result.LLMResultBuffer`
  — head/tail truncation of tool result strings (was
  ``_MAX_RESULT_CHARS`` + free ``_truncate``).
* :class:`~ember_code.core.tools.async_fire_and_forget.AsyncFireAndForget`
  — the "schedule on the running loop or drop the coro" helper
  the persistence hooks share.
* :class:`~ember_code.core.tools.process_supervisor_locator.SupervisorRegistry`
  — instance-based process-wide locator; the module-level
  ``_default_supervisor`` slot is gone.

What's LEFT here is only the composition root — how a supervisor
holds a registry, owns the foreground slot, exposes the two
execution paths (``run_backgrounded`` / ``run_foregrounded``), and
delegates log-store configuration.

NOTE (audit anchor): the OOP reference implementation for
``core/tools/`` is this file + ``process_registry.py`` together.
Sibling helper modules (``orchestrate.py``, ``plan.py``,
``todo.py``, …) should mirror this shape — instance state,
no module globals for mutable data, no ``hasattr`` duck-checks,
each named concern on a named class.

Threading model:

* :class:`ProcessRegistry` is sync-lock guarded — safe from both
  async tools and the sync ``cancel_foreground`` path.
* The foreground slot uses a separate ``threading.Lock`` and is
  driven exclusively via :meth:`ProcessSupervisor.foreground`
  (a ``@contextmanager``) so ``set_foreground`` /
  ``clear_foreground`` never diverge.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.tools.process_registry import (
    DEFAULT_FINISHED_PROCESS_TTL_SECONDS,
    ProcessRegistry,
)
from ember_code.core.tools.tool_result import LLMResultBuffer

# Re-export ProcessRegistry for backwards compatibility — the class
# moved to :mod:`process_registry` in the OOP split, but callers
# that ``from ember_code.core.tools.process_supervisor import
# ProcessRegistry`` should keep working.
__all__ = ["DEFAULT_FINISHED_PROCESS_TTL_SECONDS", "ProcessRegistry", "ProcessSupervisor"]

if TYPE_CHECKING:
    from ember_code.core.tools.managed_process import ManagedProcess
    from ember_code.core.tools.process_log import ProcessLogStore

logger = logging.getLogger(__name__)


class ProcessSupervisor:
    """Thin composition root for background-shell state.

    Owns one :class:`ProcessRegistry` (which in turn owns the bus,
    log store, persistence, TTL, and eviction bookkeeping) and
    one foreground slot. The two execution paths delegate
    lifecycle work to the registry — ``run_backgrounded`` /
    ``run_foregrounded`` call ``self.registry.announce_start(mp)``
    / ``self.registry.remove(pid)`` rather than reaching for
    private state.

    Constructor args are optional so both the module-level locator
    and fresh test instances build the same way; tests SHOULD
    build a fresh one (or call :meth:`reset`) so state doesn't
    leak across the suite.
    """

    def __init__(
        self,
        registry: ProcessRegistry | None = None,
        finished_ttl_seconds: float = DEFAULT_FINISHED_PROCESS_TTL_SECONDS,
        result_buffer: LLMResultBuffer | None = None,
    ) -> None:
        self.registry = registry or ProcessRegistry(ttl_seconds=finished_ttl_seconds)
        # Foreground slot — the ``run_shell_command`` currently
        # holding the loop. Guarded by ``_foreground_lock`` so the
        # sync :meth:`cancel_foreground` path can safely swap it
        # out while an async task is mid-wait.
        self._foreground: ManagedProcess | None = None
        self._foreground_lock = threading.Lock()
        self._result_buffer = result_buffer or LLMResultBuffer()

    # ── Log store wiring ────────────────────────────────────────

    def configure_log_store(self, project_dir: str | Path | None) -> None:
        """Wire the per-pid log store's project root. Called once
        by :meth:`RehydrateController.orphan_processes` at BE
        startup; the same store instance held by
        :class:`ManagedProcess` readers gets its root updated in
        place so open file handles opened after this call land
        under the new root."""
        self.registry.log_store.set_project_dir(project_dir)

    @property
    def log_store(self) -> ProcessLogStore:
        """Registry's log store (compat surface for readers that
        used to read ``supervisor.log_store``)."""
        return self.registry.log_store

    # ── Foreground slot ─────────────────────────────────────────

    @property
    def foreground_process(self) -> ManagedProcess | None:
        """The currently-running foreground process, or ``None``
        when the foreground slot is empty. Read-only surface for
        callers that need to know whether a foreground shell is
        active without acquiring the internal lock."""
        return self._foreground

    @contextmanager
    def foreground(self, mp: ManagedProcess) -> Iterator[None]:
        """Enter/exit ``mp`` as the active foreground process.

        The only sanctioned mutator of ``self._foreground`` — the
        foreground-cancel invariant (one FG at a time; cancel
        drops it) lives right here. ``run_foregrounded`` uses this
        via ``with supervisor.foreground(mp): ...``.
        """
        with self._foreground_lock:
            self._foreground = mp
        try:
            yield
        finally:
            with self._foreground_lock:
                if self._foreground is mp:
                    self._foreground = None

    def cancel_foreground(self) -> bool:
        """Kill the active foreground process. Called on
        Escape/cancel.

        Stays sync because the cancel path
        (``BackendServer.cancel_run``) is sync. ``proc.kill()`` is
        a sync syscall, so this works even though the process is
        owned by an async task.

        Returns True if a process was killed.
        """
        with self._foreground_lock:
            mp = self._foreground
            if mp is not None and mp.is_running():
                mp.kill()
                self._foreground = None
                return True
        return False

    # ── Command execution paths ─────────────────────────────────

    async def run_backgrounded(
        self,
        mp: ManagedProcess,
        pid: int,
        command: str,
    ) -> str:
        """Handle the ``background=True`` path of
        ``run_shell_command``.

        Auto-watches for a few seconds after spawn to capture
        startup output or an early crash. ``asyncio.sleep`` (not
        ``time.sleep``) so the event loop keeps servicing other
        work (HITL drain, FE stream) during the wait — the
        headline reason for going async.

        Distinguishes a clean fast completion (ran to completion
        inside the 3 s grace window) from a startup crash. The
        LLM consumes this string — calling a successful run
        "exited immediately" tends to nudge the model into a
        needless retry.
        """
        mp.was_backgrounded = True
        # Surface the row to subscribers + persist the DB row so
        # the watcher panel picks it up.
        self.registry.announce_start(mp)
        await asyncio.sleep(3)
        output = mp.read_new()
        if not mp.is_running():
            rc = mp.returncode()
            self.registry.remove(pid)
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

    async def run_foregrounded(
        self,
        mp: ManagedProcess,
        pid: int,
        timeout: int,
        tail: int,
    ) -> str:
        """Handle the ``background=False`` path of
        ``run_shell_command``.

        Waits up to ``timeout`` seconds for the process to
        complete. On timeout, promotes it to a background process
        (auto-background) and returns a "still running"
        description. On normal completion, waits briefly for the
        reader task to capture trailing output, then removes the
        registry entry and returns the tail.
        """
        proc = mp.proc
        timed_out = False
        with self.foreground(mp):
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True

        if timed_out:
            mp.was_backgrounded = True
            # Promoted from foreground — announce as background so
            # the watcher picks up the row.
            self.registry.announce_start(mp)
            output = mp.read(tail=tail)
            return self._result_buffer.truncate(
                f"Command still running after {timeout}s — backgrounded as PID {pid}.\n"
                f"Use read_process_output({pid}) to check output.\n"
                f"Use stop_process({pid}) to stop it.\n\n"
                f"Output so far:\n{output}"
            )

        # Command finished — wait briefly for the reader task to
        # capture any trailing output buffered after proc.wait().
        await mp.wait_for_reader(timeout=2.0)
        output = mp.read(tail=tail)
        rc = proc.returncode
        self.registry.remove(pid)

        if rc != 0:
            return self._result_buffer.truncate(f"Command exited with code {rc}:\n{output}")
        return self._result_buffer.truncate(output)

    # ── Test / shutdown helpers ─────────────────────────────────

    def reset(self) -> None:
        """Drop bus subscribers, clear the registry, unwire
        persistence, release any foreground process. Fixture-
        teardown helper — production BE never calls this."""
        self.registry.reset()
        with self._foreground_lock:
            self._foreground = None
