"""ProcessEventBus — single pub/sub API for background-process lifecycle.

Extracted from :mod:`ember_code.core.tools.shell` per
CODE_STANDARDS.md Pattern 8 + Anti-Pattern AP1 (module-level pub/sub).
Before this file, ``shell.py`` had three parallel subscriber APIs —
``subscribe_to_process_start`` / ``subscribe_to_process_line`` /
``subscribe_to_process_completion`` — each with its own list + lock
+ pair of subscribe/unsubscribe functions (12 module-level names
total). This class collapses them into one object with an
``on(event, cb)`` / ``off(event, cb)`` / ``emit(event, payload)``
interface.

``shell.py`` still exposes the old ``subscribe_to_*`` functions for
backwards compatibility (see the wrappers at the bottom of that
file), so existing callers work unchanged.

## Events

Payloads are typed :class:`ProcessEvent` union members (see
:mod:`ember_code.core.tools.process_events`). Subscribers registered
against the dict shape (pre-refactor) still work — the bus's
``emit`` accepts either a typed event or a dict, and hands each
subscriber a mapping-shaped view. Concrete types:

* ``"start"`` — :class:`ProcessStartEvent` (``{pid, cmd, started_at}``).
  Fired once when a backgrounded process is registered.
* ``"line"`` — :class:`ProcessLineEvent` (``{pid, line}``). Fired per
  stdout/stderr line. Hot path.
* ``"exit"`` — :class:`ProcessExitEvent`
  (``{pid, cmd, exit_code, duration_seconds, output_tail}``). Fired
  once when the process exits.

## Semantics

- Subscribe is idempotent — registering the same callback twice
  leaves ONE registration.
- Emit is fail-soft — a subscriber that raises is logged and the
  next subscriber still fires.
- Emit reads the subscriber list under the lock, then releases
  before firing — no lock held while a subscriber runs.

## Thread safety

Lock-guarded. Called from both the async event loop (reader tasks,
completion emits) and from FE-facing callbacks that may run on a
different thread. Acquisition is contested rarely enough that a
single ``threading.Lock`` beats per-event locks on cache footprint.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any, Union

from pydantic import BaseModel

from ember_code.core.tools.process_events import (
    EventType,
    ProcessEvent,
)

logger = logging.getLogger(__name__)

_EVENT_TYPES: tuple[EventType, ...] = ("start", "line", "exit")

# Subscribers historically accepted a ``dict`` payload. The bus now
# fires typed :class:`ProcessEvent` instances but the models are
# still mapping-like via ``.model_dump()`` — we pass the dict shape
# to subscribers so no callsite change is required. New subscribers
# can annotate against ``ProcessEvent`` directly and read fields
# typed.
Subscriber = Callable[[Any], None]

# Anything ``emit`` will accept. Typed events are the preferred
# shape; a bare ``dict`` is tolerated so legacy in-process callers
# that still hand-build the payload keep working (they get
# validated into the model before subscribers see them).
EmitPayload = Union[ProcessEvent, dict[str, Any]]  # noqa: UP007


class ProcessEventBus:
    """Fan-out subscriber bus for background-process lifecycle events.

    Instantiate once, share across the module. See module docstring
    for the event set + semantics.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[EventType, list[Subscriber]] = {e: [] for e in _EVENT_TYPES}

    def on(self, event: EventType, cb: Subscriber) -> None:
        """Register ``cb`` for ``event``. Idempotent — registering
        the same callback twice results in one registration."""
        if event not in self._subs:
            raise ValueError(f"unknown event type: {event!r}")
        with self._lock:
            if cb not in self._subs[event]:
                self._subs[event].append(cb)

    def off(self, event: EventType, cb: Subscriber) -> None:
        """Unregister ``cb`` from ``event``. No-op if not registered."""
        if event not in self._subs:
            return
        with self._lock, contextlib.suppress(ValueError):
            self._subs[event].remove(cb)

    def emit(self, event: EventType, payload: EmitPayload) -> None:
        """Fire every subscriber for ``event`` with ``payload``.

        ``payload`` may be a typed :class:`ProcessEvent` or a plain
        ``dict``. Typed events are the preferred shape (used by
        every in-repo emitter post-refactor); a bare dict is
        forwarded to subscribers as-is so pure bus unit tests can
        drive the pub/sub semantics without constructing full
        models.

        Snapshots the subscriber list under the lock, then releases
        before firing. A subscriber that raises is logged and the
        next subscriber still runs.
        """
        if event not in self._subs:
            return
        # Historical subscribers (queue injector, __main__.py fan-
        # out) read fields off a mapping. Convert typed events to
        # dicts once and forward the same shape to every
        # subscriber; dict payloads pass through unchanged (test
        # + pre-refactor caller compat).
        if isinstance(payload, BaseModel):
            # Drop the ``type`` discriminator on the wire — subscribers
            # branched on the event name (``on("line", ...)``) long
            # before the models existed and their assertions expect
            # the bare pre-refactor shape.
            dumped: Any = payload.model_dump(exclude={"type"})
        elif isinstance(payload, dict):
            dumped = payload
        else:
            logger.warning(
                "%s emit with unrecognised payload type %r — dropping",
                event,
                type(payload),
            )
            return
        with self._lock:
            subscribers = list(self._subs[event])
        for cb in subscribers:
            try:
                cb(dumped)
            except Exception as exc:
                logger.warning("%s subscriber raised: %s", event, exc)

    def subscriber_count(self, event: EventType) -> int:
        """Number of registered subscribers for ``event``. Test-only
        helper — production code doesn't need to introspect."""
        if event not in self._subs:
            return 0
        with self._lock:
            return len(self._subs[event])

    def reset(self) -> None:
        """Drop every subscriber. Test-fixture helper — session teardown
        doesn't need to call this; the bus is process-lifetime."""
        with self._lock:
            for e in _EVENT_TYPES:
                self._subs[e].clear()
