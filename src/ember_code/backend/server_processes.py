"""Background-process watcher RPCs.

Home of :class:`ProcessesController` — the OOP replacement for the
three ``BackendServer``-first-arg free functions that used to live
here (Rule 6 offender). The three wire schemas moved to
:mod:`schemas_processes` per the sibling convention; this module
now holds only the controller.

* :meth:`ProcessesController.list` — cheap "what's running"
  snapshot.
* :meth:`ProcessesController.read_tail` — safe on unknown pids.
* :meth:`ProcessesController.stop` — SIGTERM path.

The controller returns typed :mod:`schemas_processes` models. The
:class:`~ember_code.backend.server.BackendServer` facade methods
(``list_background_processes`` / ``read_process_tail`` /
``stop_background_process``) ``.model_dump()`` at the wire seam so
the JSON-RPC layer keeps seeing the dict shapes it always has —
one dump site, one wire boundary.
"""

from __future__ import annotations

import asyncio
import contextlib

from ember_code.backend.schemas_processes import (
    ProcessRow,
    ProcessTailResult,
    StopProcessResult,
)
from ember_code.core.tools.process_supervisor import ProcessSupervisor
from ember_code.core.tools.process_supervisor_locator import supervisors

# Re-export the wire schemas so existing ``from
# ember_code.backend.server_processes import ProcessRow`` style
# imports (if any surface later) keep working without an update.
__all__ = [
    "ProcessesController",
    "ProcessRow",
    "ProcessTailResult",
    "StopProcessResult",
]


class ProcessesController:
    """Background-process watcher for the process-supervisor
    registry.

    Doesn't hold a session reference because every operation reads
    from a :class:`~ember_code.core.tools.process_supervisor.ProcessSupervisor`
    — by default the process-wide supervisor from
    :attr:`~ember_code.core.tools.process_supervisor_locator.supervisors`,
    but tests can inject a stub via the ``supervisor`` constructor
    argument.
    """

    def __init__(self, supervisor: ProcessSupervisor | None = None) -> None:
        self._supervisor = supervisor or supervisors.default()

    def list(self) -> list[ProcessRow]:
        """Every running backgrounded process the registry knows
        about."""
        return [
            ProcessRow(pid=pid, cmd=cmd, elapsed_seconds=elapsed)
            for pid, cmd, elapsed in self._supervisor.registry.all_running()
        ]

    def read_tail(self, pid: int, tail: int = 200) -> ProcessTailResult:
        """Read the last ``tail`` lines from a background process's
        combined stdout/stderr buffer."""
        mp = self._supervisor.registry.get(pid)
        if mp is None:
            return ProcessTailResult(pid=pid, output="", is_running=False, exit_code=None)
        output = mp.read(tail=tail)
        exit_code = mp.returncode() if not mp.is_running() else None
        return ProcessTailResult(
            pid=pid,
            output=output,
            is_running=mp.is_running(),
            exit_code=exit_code,
        )

    async def stop(self, pid: int) -> StopProcessResult:
        """SIGTERM a background process."""
        mp = self._supervisor.registry.get(pid)
        if mp is None:
            return StopProcessResult(pid=pid, killed=False, message=f"pid {pid} not in registry")
        if not mp.is_running():
            return StopProcessResult(
                pid=pid,
                killed=False,
                message=f"pid {pid} already exited (rc={mp.returncode()})",
            )
        mp.kill()
        # Orphan path: there's no reader task waiting on ``waitpid``
        # to notice the exit + remove from registry + delete the DB
        # row. Do both explicitly.
        #
        # Polymorphic discriminator — both
        # :class:`~ember_code.core.tools.managed_process.ManagedProcess`
        # and :class:`~ember_code.core.tools.orphan_process.OrphanProcess`
        # both declare ``is_orphan`` unconditionally (False / True), so
        # plain attribute access — not ``getattr`` with a default — is
        # the honest duck-check.
        if mp.is_orphan:
            # ``registry.remove`` fires the persist-delete + pops
            # the pid + cancels any pending eviction task in one
            # atomic step — the orphan path used to do these
            # separately.
            self._supervisor.registry.remove(pid)
            return StopProcessResult(pid=pid, killed=True, message=f"pid {pid} killed")
        # Live-process path — await the reader task so the final
        # tail is flushed before the caller reads back.
        reader = getattr(mp, "_reader_task", None)
        if reader is not None:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(reader, timeout=2.0)
        return StopProcessResult(pid=pid, killed=True, message=f"pid {pid} killed")
