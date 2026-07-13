"""Orphan process types + boot-time rehydration.

Extracted from ``core/tools/shell.py``. Handles processes
that survived a previous BE lifetime — the OS pipes are gone
(so stdout can't be reattached) but the pid + pgid are
persisted in ``state.db`` so we can still probe liveness,
render the row in the watcher panel, and kill via SIGTERM.

Public surface (re-exported from ``shell.py`` for backwards
compat with existing imports):

* :class:`_OrphanProcess` — duck-types :class:`_ManagedProcess`
  enough to plug into ``_registry`` without special cases at
  every read/kill call site.
* :class:`_OrphanProcStub` — two-field stand-in for
  :class:`asyncio.subprocess.Process`.
* :func:`rehydrate_orphan_processes` — startup pass: read the
  persisted rows, probe liveness, inject alive orphans into
  the process registry, prune dead ones from the DB.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Any

from ember_code.core.tools import process_log
from ember_code.core.tools.process_store import BackgroundProcessStore

logger = logging.getLogger(__name__)


class _OrphanProcess:
    """A process the previous BE lifetime spawned that survived
    restart. Quacks like :class:`_ManagedProcess` so the registry
    treats both kinds uniformly, but without a live ``proc`` —
    the OS pipes are gone, so stdout can't be reattached.

    What we CAN do: probe liveness via ``os.kill(pid, 0)``, show
    the row + elapsed time, and kill via the saved ``pgid``. The
    log tail returns a placeholder explaining the gap.

    Used only at startup-rehydration time; a fresh spawn always
    produces a real :class:`_ManagedProcess`.
    """

    __slots__ = (
        "pid",
        "cmd",
        "pgid",
        "_started_epoch",
        "_finished",
        "was_backgrounded",
        "output",
        "_reader_task",
        "_eviction_task",
    )

    def __init__(self, pid: int, cmd: str, started_epoch: int, pgid: int | None) -> None:
        self.pid = pid
        self.cmd = cmd
        self.pgid = pgid
        self._started_epoch = started_epoch
        self._finished = False
        self.was_backgrounded = True
        # Fields the registry / RPCs read but the orphan can't
        # populate. Empty buffer + nil tasks keep duck-typing
        # working without special cases at every call site.
        self.output: list[str] = []
        self._reader_task = None
        self._eviction_task = None

    # The registry reads ``proc.pid`` and ``proc.returncode``.
    # Expose an object that matches the asyncio.subprocess
    # surface on those two attributes.
    @property
    def proc(self):  # type: ignore[no-untyped-def]
        return _OrphanProcStub(self.pid, returncode=None if not self._finished else -1)

    def is_running(self) -> bool:
        if self._finished:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            self._finished = True
            return False
        except OSError:
            return False

    def returncode(self) -> int | None:
        # Unknown for orphans — the exit status was reaped by
        # init / launchd, not us. ``None`` means "still running"
        # per the asyncio contract; we return a sentinel ``-1``
        # once dead so the FE renders "exit ?" not "running".
        return None if self.is_running() else -1

    def kill(self) -> None:
        """Send SIGTERM to the orphan. Tries the process group
        first (so child processes the orphan spawned go down
        together) then the pid itself as a fallback."""
        if self.pgid is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(self.pgid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(self.pid, signal.SIGTERM)

    def read(self, tail: int = 100) -> str:
        # Read history from the per-pid log file that the
        # previous BE's reader task tee'd to. If the file
        # exists, that IS the output — same content the live
        # FE saw before restart. If it's gone (eviction TTL,
        # disk pruned, never written) fall back to a
        # placeholder so the watcher pane has something to
        # render.
        content = process_log.tail(
            process_log.log_path(self.pid, process_log.get_default_project_dir()),
            n=tail,
        )
        if content:
            return content
        started_h = time.strftime("%H:%M:%S", time.localtime(self._started_epoch))
        return (
            f"(no buffered output — this process was started by a previous "
            f"BE lifetime at {started_h} and the per-pid log file is empty "
            f"or has been pruned. The Kill button still works.)"
        )

    def read_new(self, max_lines: int = 200) -> str:
        return ""

    @property
    def started_epoch(self) -> int:
        return self._started_epoch


@dataclass
class _OrphanProcStub:
    """Two-field stub that matches the bits of
    ``asyncio.subprocess.Process`` the registry / RPCs read on
    a real :class:`_ManagedProcess`. Kept in this module so the
    test suite can construct one directly if needed."""

    pid: int
    returncode: int | None


async def rehydrate_orphan_processes(project_dir: Any) -> int:
    """Read persisted background-process rows from the project's
    state.db, probe each pid for liveness, inject the alive ones
    into the registry as :class:`_OrphanProcess` instances. Dead
    rows are pruned from the DB during the same pass.

    Returns the count of orphans surfaced — callers can log it
    so the user can see "3 orphan processes restored from
    previous session" in BE startup logs.

    Called from :meth:`BackendServer.startup` after the
    persistence layer is up. Safe to call multiple times: the
    registry's ``add`` is idempotent on pid, and the probe
    handles a pid already in-flight (it won't surface twice).
    """
    # Late imports so this module doesn't create an
    # ``orphan → shell → orphan`` cycle at import time — the
    # registry and store setter live on ``shell.py``.
    from ember_code.core.tools.shell import _registry, set_process_store

    try:
        store = BackgroundProcessStore(project_dir=project_dir)
    except Exception as exc:
        logger.debug("orphan rehydrate: store init failed: %s", exc)
        return 0
    set_process_store(store)

    try:
        rows = await store.list_all()
    except Exception as exc:
        logger.debug("orphan rehydrate: list_all failed: %s", exc)
        return 0

    surfaced = 0
    for row in rows:
        # Liveness probe — ``os.kill(pid, 0)`` is the canonical
        # check. ProcessLookupError means dead; permission errors
        # are treated as "alive but not ours" (still worth
        # showing because we can at least try to kill via the
        # pgid).
        alive = True
        try:
            os.kill(row.pid, 0)
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        except OSError:
            alive = False

        if not alive:
            with contextlib.suppress(Exception):
                await store.remove(row.pid)
            continue

        # Skip pids the in-process registry already tracks —
        # shouldn't happen on a clean boot (the registry is
        # fresh) but defends against the BE somehow already
        # having spawned the same pid (impossible in practice).
        if _registry.get(row.pid) is not None:
            continue

        orphan = _OrphanProcess(
            pid=row.pid,
            cmd=row.cmd,
            started_epoch=row.started_at,
            pgid=row.pgid,
        )
        _registry.add(orphan)  # type: ignore[arg-type]
        surfaced += 1

    if surfaced:
        logger.info(
            "orphan rehydrate: surfaced %d background process(es) from prior BE lifetime",
            surfaced,
        )
    return surfaced
