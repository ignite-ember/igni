"""Background-process watcher RPCs.

Extracted from :mod:`ember_code.backend.server`. Three free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates:

* :func:`list_background_processes` — cheap "what's running"
  snapshot for the watcher panel header. Runs-only; the
  per-process ``process_exited`` push has already flipped
  finished rows to "stopped" before TTL eviction removes
  them from the registry.
* :func:`read_process_tail` — safe on unknown pids and on
  exited-but-not-evicted rows. The FE polls this every time
  the panel is open, so this must never raise.
* :func:`stop_background_process` — SIGTERM path.
  ``killed=False`` for unknown-pid or already-exited rows so
  the FE can render an accurate toast rather than a fake
  success.

Wire returns go through Pydantic models (Rule 1) — every
function builds a typed ``*Result`` and ``.model_dump()``s at
the return statement. FE tests still see plain dicts on the
receiving end, but the shape is defined once here.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.core.tools.shell import _OrphanProcess, _persist_remove, _registry

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer


class ProcessTailResult(BaseModel):
    """Wire shape for :func:`read_process_tail`."""

    pid: int
    output: str
    is_running: bool
    exit_code: int | None


class ProcessRow(BaseModel):
    """One row of :func:`list_background_processes`."""

    pid: int
    cmd: str
    elapsed_seconds: float


class StopProcessResult(BaseModel):
    """Wire shape for :func:`stop_background_process`."""

    pid: int
    killed: bool
    message: str


def list_background_processes(backend: "BackendServer") -> list[dict]:  # noqa: ARG001
    """Every running backgrounded process the registry knows
    about. Each entry: ``{pid, cmd, elapsed_seconds}``.
    Finished-but-not-evicted processes are intentionally
    omitted — the watcher only shows live work; the per-
    process ``process_exited`` push has already flipped any
    row to "stopped" before TTL eviction removes it.
    """
    return [
        ProcessRow(pid=pid, cmd=cmd, elapsed_seconds=elapsed).model_dump()
        for pid, cmd, elapsed in _registry.all_running()
    ]


def read_process_tail(
    backend: "BackendServer",  # noqa: ARG001
    pid: int,
    tail: int = 200,
) -> dict:
    """Read the last ``tail`` lines from a background process's
    combined stdout/stderr buffer. Safe on unknown pids and on
    exited-but-not-evicted rows — the FE polls this whenever
    the panel is open, so it must never raise."""
    mp = _registry.get(pid)
    if mp is None:
        return ProcessTailResult(
            pid=pid, output="", is_running=False, exit_code=None
        ).model_dump()
    output = mp.read(tail=tail)
    exit_code = mp.returncode() if not mp.is_running() else None
    return ProcessTailResult(
        pid=pid,
        output=output,
        is_running=mp.is_running(),
        exit_code=exit_code,
    ).model_dump()


async def stop_background_process(
    backend: "BackendServer",  # noqa: ARG001
    pid: int,
) -> dict:
    """SIGTERM a background process. Returns ``{pid, killed,
    message}`` — ``killed=False`` for unknown-pid or
    already-exited rows so the FE can render an accurate
    toast rather than a fake success.
    """
    mp = _registry.get(pid)
    if mp is None:
        return StopProcessResult(
            pid=pid, killed=False, message=f"pid {pid} not in registry"
        ).model_dump()
    if not mp.is_running():
        return StopProcessResult(
            pid=pid,
            killed=False,
            message=f"pid {pid} already exited (rc={mp.returncode()})",
        ).model_dump()
    mp.kill()
    # Orphan path: there's no reader task waiting on ``waitpid``
    # to notice the exit + remove from registry + delete the DB
    # row. Do both explicitly so the panel sees the row disappear
    # after the kill (matches the live-process behaviour where
    # the reader task cleans up).
    if isinstance(mp, _OrphanProcess):
        _registry.remove(pid)
        _persist_remove(pid)
        return StopProcessResult(
            pid=pid, killed=True, message=f"pid {pid} killed"
        ).model_dump()
    # Live-process path — await the reader task so the final
    # tail is flushed before the caller reads back. Bounded so a
    # stuck reader can't hang the whole RPC.
    reader = getattr(mp, "_reader_task", None)
    if reader is not None:
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(reader, timeout=2.0)
    return StopProcessResult(pid=pid, killed=True, message=f"pid {pid} killed").model_dump()
