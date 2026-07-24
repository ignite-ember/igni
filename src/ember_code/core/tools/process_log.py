"""Per-pid log files for backgrounded shell processes.

The in-memory ``ManagedProcess.output`` buffer is the live source
of truth — fast, lock-protected, hot-path-friendly. But it dies
with the BE. The watcher's orphan-rehydrate path (BE restart →
process survived) can't reach it.

This module gives backgrounded processes a durable companion: a
single tail-able log file per pid under
``<project_dir>/.ember/process_logs/<pid>.log``. The reader task
appends each decoded line to the file alongside the in-memory
buffer; :meth:`~ember_code.core.tools.orphan_process.OrphanProcess.read`
reads from the file when the in-memory buffer is gone.

OOP anchor: this file is the OOP reference for per-pid log
storage. :class:`ProcessLogStore` owns a single ``project_dir``
field and exposes :meth:`path`, :meth:`open`, :meth:`tail`,
:meth:`cleanup` as instance methods — no free functions, no
module-level mutable state. The store is owned as
``ProcessSupervisor.log_store`` so every consumer reaches it via
the supervisor rather than a module global.

Lifecycle of a :class:`ProcessLogStore`:

* Constructor takes ``project_dir: str | Path | None``. ``None``
  falls back to ``$TMPDIR/ember-process-logs/`` so the store is
  functional in test / headless contexts without forcing every
  caller to plumb a path.
* :meth:`path` — deterministic path for a pid. Both writer +
  orphan reader resolve it the same way.
* :meth:`open` — opens the file in append mode, creating parent
  dirs if needed. Returned object is closeable; the reader holds
  it for the process's lifetime.
* :meth:`tail` — read last ``n`` lines without slurping the
  whole file. Used by the orphan's ``read`` method.
* :meth:`cleanup` — delete the file. Called from the registry's
  TTL eviction path so finished processes don't leak files
  indefinitely.
* :meth:`set_project_dir` — mutation is gated to a single setter,
  called only during rehydrate wiring by
  :meth:`ProcessSupervisor.configure_log_store`, never in steady
  state.

The path is stable for the pid's lifetime, but pid reuse across
restarts is technically possible — we accept that the new
spawn's log file will start with leftover content from a
previous incarnation, which the writer's append-only mode
preserves until cleanup runs.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class ProcessLogStore:
    """Owns the per-pid log file layout under a single project root.

    The ``project_dir`` is an instance field, not a module global —
    every consumer that needs per-pid logging holds a reference to
    a store (typically via ``ProcessSupervisor.log_store``) rather
    than reaching for a module-level default.
    """

    def __init__(self, project_dir: str | Path | None = None) -> None:
        self._project_dir: str | Path | None = project_dir

    @property
    def project_dir(self) -> str | Path | None:
        """Current project root the store writes under. ``None``
        means "fall back to TMPDIR"."""
        return self._project_dir

    def set_project_dir(self, project_dir: str | Path | None) -> None:
        """Rewire the store to a new project root. Called only
        during rehydrate wiring by
        :meth:`ProcessSupervisor.configure_log_store`, never in
        steady state — the ``ManagedProcess`` reader holds an open
        file handle from :meth:`open` for the process's lifetime,
        so changing the root mid-flight would strand its writes
        under the old path. In practice ``configure_log_store``
        runs once at BE startup before any process is spawned."""
        self._project_dir = project_dir

    def path(self, pid: int) -> Path:
        """Return ``<project_dir>/.ember/process_logs/<pid>.log``.

        Falls back to ``$TMPDIR/ember-process-logs/<pid>.log`` when
        no ``project_dir`` was supplied — keeps the writer
        functional in test / headless contexts without forcing
        every caller to plumb a path.
        """
        if self._project_dir is None:
            root = Path(tempfile.gettempdir()) / "ember-process-logs"
        else:
            root = Path(str(self._project_dir)) / ".ember" / "process_logs"
        return root / f"{int(pid)}.log"

    def open(self, pid: int) -> io.TextIOBase | None:
        """Open the per-pid log file in append mode. Returns
        ``None`` when the open fails (read-only filesystem,
        permission error) — callers fall through to in-memory-only
        behaviour, same as before the log files existed. Best-
        effort.

        Line-buffered (``buffering=1``) so each ``write("…\\n")``
        flushes to disk immediately — without that flush an orphan
        reading mid-stream would see stale tails up to the OS
        buffer's flush cadence.
        """
        path = self.path(pid)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return open(path, "a", encoding="utf-8", buffering=1, errors="replace")
        except OSError as exc:
            logger.debug("process log open failed for pid=%s: %s", pid, exc)
            return None

    def tail(self, pid: int, n: int = 200) -> str:
        """Return the last ``n`` lines of the pid's log file as a
        single ``\\n``-joined string. Returns an empty string when
        the file is missing or unreadable — orphan reads default
        to "no output yet" rather than crashing the watcher.

        Reads the whole file then slices the tail. The file is
        bounded by the writer's natural log volume (a chatty
        dev server might be MBs but not GBs); a more clever
        seek-from-end is possible if we ever hit that ceiling.
        """
        if n <= 0:
            return ""
        path = self.path(pid)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            logger.debug("process log tail failed for %s: %s", path, exc)
            return ""
        if not lines:
            return ""
        return "".join(lines[-n:]).rstrip("\n")

    def cleanup(self, pid: int) -> None:
        """Best-effort delete of a pid's log file. Idempotent —
        no-op when the file's already gone."""
        path = self.path(pid)
        with contextlib.suppress(FileNotFoundError, OSError):
            os.remove(path)
