"""Wire schemas for the background-process watcher RPCs.

Companion module to :mod:`server_processes` — follows the sibling
``schemas_*.py`` convention (see :mod:`schemas_run`,
:mod:`schemas_hitl`, :mod:`schemas_panels`, etc.) so every RPC
response served by :class:`~ember_code.backend.server_processes.ProcessesController`
is a typed Pydantic model rather than a raw dict.

Contains:

* :class:`ProcessRow` — one row of the running-process list.
* :class:`ProcessTailResult` — typed shape for
  :meth:`~ember_code.backend.server_processes.ProcessesController.read_tail`.
* :class:`StopProcessResult` — typed shape for
  :meth:`~ember_code.backend.server_processes.ProcessesController.stop`.

The controller returns these models directly; the
:class:`~ember_code.backend.server.BackendServer` facade methods
``.model_dump()`` at the wire seam so the JSON-RPC layer keeps
seeing the same dict shapes it always has.
"""

from __future__ import annotations

from pydantic import BaseModel


class ProcessRow(BaseModel):
    """One row of :meth:`ProcessesController.list`."""

    pid: int
    cmd: str
    elapsed_seconds: float


class ProcessTailResult(BaseModel):
    """Wire shape for :meth:`ProcessesController.read_tail`."""

    pid: int
    output: str
    is_running: bool
    exit_code: int | None


class StopProcessResult(BaseModel):
    """Wire shape for :meth:`ProcessesController.stop`."""

    pid: int
    killed: bool
    message: str
