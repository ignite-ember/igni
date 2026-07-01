"""Per-pid log files for backgrounded shell processes.

The in-memory ``_ManagedProcess.output`` buffer is the live source
of truth — fast, lock-protected, hot-path-friendly. But it dies
with the BE. The watcher's orphan-rehydrate path (BE restart →
process survived) can't reach it.

This module gives backgrounded processes a durable companion: a
single tail-able log file per pid under
``<project_dir>/.ember/process_logs/<pid>.log``. The reader task
appends each decoded line to the file alongside the in-memory
buffer; ``_OrphanProcess.read()`` reads from the file when the
in-memory buffer is gone.

Lifecycle:

* ``open_log(pid, project_dir)`` — opens the file in append mode,
  creating parent dirs if needed. Returned object is closeable;
  the reader holds it for the process's lifetime.
* ``log_path(pid, project_dir)`` — deterministic path for a pid.
  Both writer + orphan reader resolve it the same way.
* ``tail(path, n)`` — read last ``n`` lines without slurping the
  whole file. Used by the orphan's ``read`` method.
* ``cleanup(pid, project_dir)`` — delete the file. Called from
  the registry's TTL eviction path so finished processes don't
  leak files indefinitely.

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
from pathlib import Path

logger = logging.getLogger(__name__)


def log_path(pid: int, project_dir: str | Path | None) -> Path:
    """Return ``<project_dir>/.ember/process_logs/<pid>.log``.

    Falls back to ``$TMPDIR/ember-process-logs/<pid>.log`` when
    no project_dir is supplied — keeps the writer functional in
    test / headless contexts without forcing every caller to
    plumb a path.
    """
    if project_dir is None:
        import tempfile

        root = Path(tempfile.gettempdir()) / "ember-process-logs"
    else:
        root = Path(str(project_dir)) / ".ember" / "process_logs"
    return root / f"{int(pid)}.log"


def open_log(pid: int, project_dir: str | Path | None) -> io.TextIOBase | None:
    """Open the per-pid log file in append mode. Returns ``None``
    when the open fails (read-only filesystem, permission error)
    — callers fall through to in-memory-only behaviour, same as
    before the log files existed. Best-effort.

    Line-buffered (``buffering=1``) so each ``write("…\\n")`` flushes
    to disk immediately — without that flush an orphan reading
    mid-stream would see stale tails up to the OS buffer's
    flush cadence.
    """
    path = log_path(pid, project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "a", encoding="utf-8", buffering=1, errors="replace")
    except OSError as exc:
        logger.debug("process log open failed for pid=%s: %s", pid, exc)
        return None


def tail(path: Path, n: int = 200) -> str:
    """Return the last ``n`` lines of ``path`` as a single
    ``\\n``-joined string. Returns an empty string when the file
    is missing or unreadable — orphan reads default to "no
    output yet" rather than crashing the watcher.

    Reads the whole file then slices the tail. The file is
    bounded by the writer's natural log volume (a chatty
    dev server might be MBs but not GBs); a more clever
    seek-from-end is possible if we ever hit that ceiling.
    """
    if n <= 0:
        return ""
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


def cleanup(pid: int, project_dir: str | Path | None) -> None:
    """Best-effort delete of a pid's log file. Idempotent —
    no-op when the file's already gone."""
    path = log_path(pid, project_dir)
    with contextlib.suppress(FileNotFoundError, OSError):
        os.remove(path)


# Module-level default for project_dir — set by BackendServer at
# startup so the shell tool's hot path doesn't have to plumb the
# value through ``EmberShellTools.__init__``. ``None`` means
# "fall back to TMPDIR" which is good enough for tests.

_default_project_dir: str | Path | None = None


def set_default_project_dir(project_dir: str | Path | None) -> None:
    """Wire the per-pid log path's project root. Called once at
    BE startup; same shape as ``shell.set_process_store``."""
    global _default_project_dir
    _default_project_dir = project_dir


def get_default_project_dir() -> str | Path | None:
    return _default_project_dir
