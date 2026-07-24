"""Typed Pydantic event models for the process-lifecycle event bus.

Replaces the anonymous ``dict[str, Any]`` payloads that
:class:`ember_code.core.tools.process_bus.ProcessEventBus` used to
fire. Rule 1 (typed schemas over raw dicts) + Pattern 2
(discriminated unions instead of adhoc dict shapes).

Three events; the discriminator field is ``type``:

* :class:`ProcessStartEvent` — ``type="start"``. Fired once when a
  backgrounded process is registered.
* :class:`ProcessLineEvent` — ``type="line"``. Fired per stdout/
  stderr line. Hot path — construction cost matters.
* :class:`ProcessExitEvent` — ``type="exit"``. Fired once when the
  process exits (cleanly or on kill).

``EventType`` moved here from ``process_bus.py`` so the bus and the
payload models share one source of truth.

Subscribers receive a :class:`ProcessEvent` — the tagged union — and
can either branch on ``event.type`` or access typed fields via
``pattern matching``. Legacy subscribers written against the dict
payload keep working: :class:`~ember_code.core.tools.process_bus.ProcessEventBus`
still hands off a mapping-shaped view because the models are plain
:class:`pydantic.BaseModel` instances (see the bus's ``emit`` path
for details on the dict-emit shim).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal["start", "line", "exit"]


class ProcessStartEvent(BaseModel):
    """A backgrounded process has been registered.

    Payload matches the pre-refactor dict:
    ``{pid, cmd, started_at}`` where ``started_at`` is a wall-clock
    epoch (``time.time()``), not a monotonic value — subscribers use
    it for FE display so it must survive process boundaries.
    """

    # ``.model_construct()`` allowed on hot paths; construction still
    # needs to be cheap.
    model_config = ConfigDict(frozen=True)

    type: Literal["start"] = "start"
    pid: int
    cmd: str
    started_at: float


class ProcessLineEvent(BaseModel):
    """One stdout/stderr line from the reader task. Hot path — the
    reader may fire thousands per second on a chatty log."""

    model_config = ConfigDict(frozen=True)

    type: Literal["line"] = "line"
    pid: int
    line: str


class ProcessExitEvent(BaseModel):
    """A backgrounded process has exited.

    ``exit_code`` is ``None`` for orphans whose exit status was
    reaped by init/launchd rather than us. ``duration_seconds`` uses
    monotonic elapsed for live processes; for orphans the reader
    task doesn't fire this event at all (orphans never emit ``exit``
    because we never got the process handle to ``wait`` on).
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["exit"] = "exit"
    pid: int
    cmd: str
    exit_code: int | None
    duration_seconds: float
    output_tail: str


# Discriminated union — subscribers switch on ``.type`` to narrow.
ProcessEvent = Annotated[
    ProcessStartEvent | ProcessLineEvent | ProcessExitEvent,
    Field(discriminator="type"),
]


# Typed subscriber contract. Lives here (next to the payload
# models) rather than in ``shell.py`` per the OOP audit's
# data-and-behavior-together finding — the callback contract
# belongs alongside the discriminated union it consumes.
#
# NOTE: :class:`ember_code.core.tools.process_bus.ProcessEventBus`
# currently normalises typed events to ``dict`` before firing
# subscribers (a wire-compat shim retained for the legacy
# push-bridge handlers that read fields via ``info["pid"]``).
# New subscribers should still accept ``dict`` at runtime; the
# alias here documents the intended payload SHAPE and lets tools
# like mypy narrow on it once the dict-emit shim is removed.
ProcessEventCallback = Callable[[ProcessEvent], None]
