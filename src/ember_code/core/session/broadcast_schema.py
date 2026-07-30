"""Typed schema for the session broadcast bus.

Sibling to :mod:`event_log_schema` — one shape per subsystem,
one file per shape. :class:`BroadcastEvent` is the wire shape
:class:`ember_code.core.session.broadcast.BroadcastBus` fans out
to callbacks.

Uses a stdlib ``@dataclass`` (not a Pydantic model) so the
``payload: dict[str, Any]`` field survives ``BroadcastEvent(...)``
construction by identity — callbacks receive the same live dict
the emitter passed in. Pydantic v2's ``model_validate`` clones
dicts, which breaks the identity contract several tests pin
against (see :meth:`BroadcastBus.emit`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

BroadcastCallback = Callable[[str, dict[str, Any]], None]
"""Subscriber signature. Kept as ``(channel, payload)`` for
transport-adapter compatibility — the bus unpacks the event on
the way out so internal callers get typed
:class:`BroadcastEvent` construction while external subscribers
keep the two-argument tuple signature."""


@dataclass(frozen=True)
class BroadcastEvent:
    """One push-channel event.

    * ``channel`` — string tag the FE routes on
      (``"plan_submitted"``, ``"permission_mode_changed"``, …).
      Producer-open — new tools introduce new channels without a
      schema change.
    * ``payload`` — event-specific dict. Delivered to callbacks
      by identity; a callback that mutates it sees the same live
      dict the emitter did.

    ``frozen=True`` guards accidental reassignment of the two
    fields; the payload itself is intentionally mutable — the
    identity contract requires the same live dict object to
    survive from emit call to callback receipt.
    """

    channel: str
    payload: dict[str, Any] = field(default_factory=dict)

    def with_run_id(self, run_id: str | None) -> BroadcastEvent:
        """Return an event whose payload carries ``run_id``.

        The drain path calls this so ``plan_submitted`` payloads
        acquire the run_id the FE needs for ``approve_plan`` /
        ``dismiss_plan``.

        Contract:

        * Empty / ``None`` ``run_id`` → return ``self`` unchanged.
        * Payload already carries ``run_id`` → return ``self``
          unchanged (an explicit value from the emitter wins over
          an implicit stamp from the drain context).
        * Otherwise return a new :class:`BroadcastEvent` with a
          shallow-copied payload plus ``run_id`` — the source
          event is not mutated.
        """
        if not run_id:
            return self
        if "run_id" in self.payload:
            return self
        return BroadcastEvent(
            channel=self.channel,
            payload={**self.payload, "run_id": run_id},
        )
