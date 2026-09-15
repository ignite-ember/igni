"""A background process that has already ended, read back from disk.

:class:`~ember_code.core.tools.orphan_process.OrphanProcess` represents
a process from a previous BE lifetime that is *still running* — it
probes the pid and can kill it. This is its counterpart for one that is
not: a row the store recorded an ending for.

It exists because "finished" and "gone" stopped being the same thing.
The watcher used to list only what was running, so a command that had
just failed was filtered out of the panel someone opens to find out why,
and a BE restart erased the lot. A finished row now survives, and the
registry needs something to hold that answers the same small surface the
watcher reads — ``is_running``, ``returncode``, ``cmd``, ``elapsed``,
``read`` — without pretending there is a process behind it.

Deliberately not a subclass of ``OrphanProcess``: nearly every method
there is about liveness, and inheriting them to override each with
"no, it's over" would be a worse description of the thing than a small
class that says so once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.core.tools.process_log import ProcessLogStore
    from ember_code.core.tools.process_store import BackgroundProcessRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinishedProcStub:
    """Stands in for ``mp.proc`` so registry code that reaches for
    ``mp.proc.pid`` / ``.returncode`` works unchanged."""

    pid: int
    returncode: int | None


class FinishedProcess:
    """A completed process, reconstructed from its persisted row."""

    __slots__ = ("_pid", "_cmd", "_started_epoch", "_finished_epoch", "_exit_code", "_log_store")

    def __init__(
        self,
        pid: int,
        cmd: str,
        started_epoch: int,
        finished_epoch: int | None,
        exit_code: int | None,
        log_store: ProcessLogStore | None = None,
    ) -> None:
        self._pid = pid
        self._cmd = cmd
        self._started_epoch = started_epoch
        self._finished_epoch = finished_epoch
        self._exit_code = exit_code
        self._log_store = log_store

    @classmethod
    def from_row(
        cls,
        row: BackgroundProcessRow,
        log_store: ProcessLogStore | None = None,
    ) -> FinishedProcess:
        return cls(
            pid=row.pid,
            cmd=row.cmd,
            started_epoch=row.started_at,
            finished_epoch=row.finished_at,
            exit_code=row.exit_code,
            log_store=log_store,
        )

    # ── The surface the registry and watcher read ───────────────

    @property
    def cmd(self) -> str:
        return self._cmd

    @property
    def proc(self) -> FinishedProcStub:
        return FinishedProcStub(pid=self._pid, returncode=self._exit_code)

    def is_running(self) -> bool:
        return False

    def returncode(self) -> int | None:
        """The recorded exit code, or ``None`` when the BE exited
        before it could observe the ending. ``None`` is honest here and
        the watcher renders it as an unknown — better than inventing a
        code for something nobody saw."""
        return self._exit_code

    def elapsed(self) -> float:
        """How long it ran, not how long ago it ended."""
        if self._finished_epoch is None:
            return 0.0
        return max(0.0, float(self._finished_epoch - self._started_epoch))

    def kill(self) -> None:
        """Nothing to kill. Present so the registry's teardown paths
        can call it without a hasattr dance."""

    def read(self, tail: int = 100) -> str:
        store = self._log_store
        if store is None:
            return ""
        try:
            return store.read(self._pid, tail=tail)
        except Exception:  # noqa: BLE001 — a missing log is not an error here
            logger.debug("no stored log for finished pid %s", self._pid)
            return ""

    def read_new(self, max_lines: int = 200) -> str:
        """Nothing new will ever arrive."""
        return ""
