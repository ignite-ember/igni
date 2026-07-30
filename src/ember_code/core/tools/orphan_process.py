"""OrphanProcess — a background shell process that outlived its BE.

Extracted from :mod:`shell_orphan` per the OOP audit. This module
is the single home of the orphan model — the class owns its
liveness probe (``os.kill(pid, 0)``), its log-file read, and its
private ``_finished`` sticky bit, as instance methods rather than
scattered branches on a coordinator.

Public API:

* :class:`OrphanProcess` — duck-types :class:`ManagedProcess`
  enough to plug into :class:`ProcessRegistry` without special
  cases at every read/kill call site. The class attribute
  ``is_orphan = True`` (paired with ``is_orphan = False`` on
  :class:`ManagedProcess`) lets backend code discriminate
  polymorphically instead of ``isinstance``-checking a private
  name.
* :meth:`OrphanProcess.from_row` — classmethod constructor from a
  :class:`BackgroundProcessRow`. Preferred over the raw
  ``__init__`` because it also lets the caller inject a
  :class:`ProcessLogStore` so :meth:`OrphanProcess.read` doesn't
  reach for the module-level supervisor singleton on every call.
* :meth:`OrphanProcess.probe_alive` — classmethod that owns the
  ``os.kill(pid, 0)`` branch chain, used both from within
  :meth:`is_running` and from :class:`OrphanRehydrator` so the
  liveness policy has one source of truth.

Command / query split: the sticky ``_finished`` flag has a single
writer (:meth:`_refresh_liveness`) so :meth:`is_running` and
:meth:`returncode` become pure readers.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import time
from typing import TYPE_CHECKING

from ember_code.core.tools.process_supervisor_locator import supervisors
from ember_code.core.tools.shell_orphan_schemas import (
    OrphanProcStub,
    OrphanReadResult,
)

if TYPE_CHECKING:
    import asyncio

    from ember_code.core.tools.process_log import ProcessLogStore
    from ember_code.core.tools.process_store import BackgroundProcessRow

logger = logging.getLogger(__name__)


class OrphanProcess:
    """A process the previous BE lifetime spawned that survived
    restart. Quacks like :class:`ManagedProcess` so the registry
    treats both kinds uniformly, but without a live ``proc`` —
    the OS pipes are gone, so stdout can't be reattached.

    What we CAN do: probe liveness via ``os.kill(pid, 0)``, show
    the row + elapsed time, and kill via the saved ``pgid``. The
    log tail returns a placeholder explaining the gap when the
    on-disk log is empty.

    Used only at startup-rehydration time; a fresh spawn always
    produces a real :class:`ManagedProcess`.

    Class attribute ``is_orphan = True`` pairs with
    :attr:`ManagedProcess.is_orphan` (``False``) so backend code
    stops isinstance-checking a private name. Read from
    :meth:`ProcessesController.stop`.
    """

    #: Polymorphic discriminator. Backend code can branch on
    #: ``mp.is_orphan`` without importing this class or reaching
    #: for :func:`isinstance`.
    is_orphan: bool = True

    __slots__ = (
        "pid",
        "cmd",
        "pgid",
        "_started_epoch",
        "_finished",
        "was_backgrounded",
        "output",
        "_reader_task",
        "_log_store",
    )

    def __init__(
        self,
        pid: int,
        cmd: str,
        started_epoch: int,
        pgid: int | None,
        log_store: ProcessLogStore | None = None,
    ) -> None:
        self.pid = pid
        self.cmd = cmd
        self.pgid = pgid
        self._started_epoch = started_epoch
        self._finished = False
        self.was_backgrounded = True
        # Fields the registry / RPCs read but the orphan can't
        # populate. Empty buffer + nil reader task keeps duck-
        # typing working without special cases at every call site.
        # (Eviction task handles live on
        # :class:`ProcessRegistry` — orphans don't need to expose
        # an ``_eviction_task`` slot anymore.)
        self.output: list[str] = []
        # Explicit annotation matches :class:`ManagedProcess` so
        # mypy/AP5 doesn't fall back to inferring ``None``.
        self._reader_task: asyncio.Task[None] | None = None
        # Injected log store — replaces the module-level
        # ``supervisors.default().log_store`` reach-in on every
        # :meth:`read`. When ``None`` we fall back to the
        # supervisor default at read time so pre-existing callers
        # (and tests that build a bare ``OrphanProcess(...)``) keep
        # working during the deprecation window.
        self._log_store: ProcessLogStore | None = log_store

    # ── Classmethod constructors + probes ───────────────────────

    @classmethod
    def from_row(
        cls,
        row: BackgroundProcessRow,
        log_store: ProcessLogStore | None = None,
    ) -> OrphanProcess:
        """Build an :class:`OrphanProcess` from a persisted
        :class:`BackgroundProcessRow`. Preferred entry point over
        the raw four-positional ``__init__`` — keeps the arg count
        for callers ≤ 2 and makes log-store injection the norm.
        """
        return cls(
            pid=row.pid,
            cmd=row.cmd,
            started_epoch=row.started_at,
            pgid=row.pgid,
            log_store=log_store,
        )

    @classmethod
    def probe_alive(cls, pid: int) -> bool:
        """Single source of truth for the ``os.kill(pid, 0)``
        liveness probe.

        Semantics: ``ProcessLookupError`` and ``OSError`` mean
        dead; ``PermissionError`` means "alive but not ours"
        (worth surfacing — we can still try SIGTERM via the
        saved pgid). Called both from :meth:`_refresh_liveness`
        on an instance and from :class:`OrphanRehydrator.run`
        where no instance exists yet.
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    # ── Command / query split ───────────────────────────────────

    def _refresh_liveness(self) -> None:
        """Sole writer of ``self._finished``. Runs the probe and
        latches the sticky-dead bit. All readers (:meth:`is_running`,
        :meth:`returncode`, :meth:`proc`) route through here so the
        command/query split stays clean — no reader mutates state
        as a side effect.
        """
        if self._finished:
            return
        if not OrphanProcess.probe_alive(self.pid):
            self._finished = True

    def is_running(self) -> bool:
        self._refresh_liveness()
        return not self._finished

    def returncode(self) -> int | None:
        # Unknown for orphans — the exit status was reaped by
        # init / launchd, not us. ``None`` means "still running"
        # per the asyncio contract; we return a sentinel ``-1``
        # once dead so the FE renders "exit ?" not "running".
        self._refresh_liveness()
        return None if not self._finished else -1

    # The registry reads ``proc.pid`` and ``proc.returncode``.
    # Expose an object that matches the asyncio.subprocess
    # surface on those two attributes.
    @property
    def proc(self) -> OrphanProcStub:
        self._refresh_liveness()
        return OrphanProcStub(
            pid=self.pid,
            returncode=None if not self._finished else -1,
        )

    # ── Actions ─────────────────────────────────────────────────

    def kill(self) -> None:
        """Send SIGTERM to the orphan. Tries the process group
        first (so child processes the orphan spawned go down
        together) then the pid itself as a fallback."""
        if self.pgid is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(self.pgid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(self.pid, signal.SIGTERM)

    def elapsed(self) -> float:
        """Seconds since the process started. Uses wall-clock
        (``time.time()``) because ``_started_epoch`` came from a
        previous BE lifetime — the current process's monotonic
        origin doesn't line up. Sibling
        :meth:`~ember_code.core.tools.managed_process.ManagedProcess.elapsed`
        uses monotonic; the registry's ``all_running`` calls
        ``mp.elapsed()`` polymorphically so both flavours report
        correctly without a duck-type branch."""
        return time.time() - self._started_epoch

    def read(self, tail: int = 100) -> str:
        """Return the last ``tail`` lines of buffered output from
        the on-disk log file. Falls back to a placeholder when
        the file is missing / empty.

        Returns ``str`` (not :class:`OrphanReadResult`) to keep
        the polymorphic contract consistent with
        :meth:`ManagedProcess.read` — the same call site
        (:meth:`ProcessesController.read_tail`) hits both types.
        Callers that need the placeholder-vs-real distinction
        reach for :meth:`read_typed`.
        """
        return self.read_typed(tail=tail).content

    def read_typed(self, tail: int = 100) -> OrphanReadResult:
        """Same as :meth:`read` but returns the typed
        :class:`OrphanReadResult` so callers can distinguish
        placeholder text from real captured output without
        substring-sniffing.
        """
        store = self._resolve_log_store()
        content = store.tail(self.pid, n=tail) if store is not None else ""
        if content:
            return OrphanReadResult(content=content, is_placeholder=False)
        started_h = time.strftime("%H:%M:%S", time.localtime(self._started_epoch))
        placeholder = (
            f"(no buffered output — this process was started by a previous "
            f"BE lifetime at {started_h} and the per-pid log file is empty "
            f"or has been pruned. The Kill button still works.)"
        )
        return OrphanReadResult(content=placeholder, is_placeholder=True)

    def read_new(self, max_lines: int = 200) -> str:
        return ""

    @property
    def started_epoch(self) -> int:
        return self._started_epoch

    # ── Internals ───────────────────────────────────────────────

    def _resolve_log_store(self) -> ProcessLogStore | None:
        """Return the injected log store or fall back to the
        supervisor default.

        The fallback exists so existing tests (and the two
        remaining call sites during the deprecation window) that
        construct an :class:`OrphanProcess` without a log store
        still read from the same on-disk location the writer used.
        Prefer :meth:`from_row` with an explicit ``log_store`` for
        new code.
        """
        if self._log_store is not None:
            return self._log_store
        # Fallback: reach for the process-wide supervisor default.
        # Kept as a legacy path — new callers should inject via
        # :meth:`from_row` so we don't reach through the locator.
        return supervisors.default().log_store
