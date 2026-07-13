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

- ``"start"`` — payload ``{pid, cmd, started_at}``. Fired once when
  a backgrounded process is registered.
- ``"line"`` — payload ``{pid, line}``. Fired per stdout/stderr line.
  Hot path.
- ``"exit"`` — payload ``{pid, cmd, exit_code, duration_seconds,
  output_tail}``. Fired once when the process exits.

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
from typing import Any, Literal

logger = logging.getLogger(__name__)

EventType = Literal["start", "line", "exit"]
_EVENT_TYPES: tuple[EventType, ...] = ("start", "line", "exit")


class ProcessEventBus:
    """Fan-out subscriber bus for background-process lifecycle events.

    Instantiate once, share across the module. See module docstring
    for the event set + semantics.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[EventType, list[Callable[[dict[str, Any]], None]]] = {
            e: [] for e in _EVENT_TYPES
        }

    def on(
        self,
        event: EventType,
        cb: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register ``cb`` for ``event``. Idempotent — registering
        the same callback twice results in one registration."""
        if event not in self._subs:
            raise ValueError(f"unknown event type: {event!r}")
        with self._lock:
            if cb not in self._subs[event]:
                self._subs[event].append(cb)

    def off(
        self,
        event: EventType,
        cb: Callable[[dict[str, Any]], None],
    ) -> None:
        """Unregister ``cb`` from ``event``. No-op if not registered."""
        if event not in self._subs:
            return
        with self._lock, contextlib.suppress(ValueError):
            self._subs[event].remove(cb)

    def emit(self, event: EventType, payload: dict[str, Any]) -> None:
        """Fire every subscriber for ``event`` with ``payload``.

        Snapshots the subscriber list under the lock, then releases
        before firing. A subscriber that raises is logged and the
        next subscriber still runs.
        """
        if event not in self._subs:
            return
        with self._lock:
            subscribers = list(self._subs[event])
        for cb in subscribers:
            try:
                cb(payload)
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
