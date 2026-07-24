"""Session-level broadcast machinery.

:class:`BroadcastBus` owns the push-channel fan-out — a callback
list plus a deferred-until-run-finish queue. Session composes
one instance (``self.broadcast_bus = BroadcastBus()``); the two
lists live inside the bus, not on Session.

Delivery contract:

* :meth:`BroadcastBus.emit` fires every callback synchronously
  with ``(event.channel, event.payload)``. Callbacks are
  expected to just enqueue a push, not to await IO. Exceptions
  in one callback don't sink the rest — each is guarded
  independently.
* :meth:`BroadcastBus.queue_post_run` defers delivery until
  the stream consumer calls :meth:`drain_post_run` (right
  after ``RunCompleted`` / ``RunError``). Used by tools whose
  card must appear at the *bottom* of a reply, not mid-stream
  — most notably ``exit_plan_mode``'s PlanCard.

:class:`BroadcastEvent` and :data:`BroadcastCallback` are
re-exported from the sibling schema module so callers keep one
import path.
"""

from __future__ import annotations

import logging

from ember_code.core.session.broadcast_schema import BroadcastCallback, BroadcastEvent

logger = logging.getLogger(__name__)


__all__ = ["BroadcastBus", "BroadcastEvent", "BroadcastCallback"]


class BroadcastBus:
    """Owns the callback list and the post-run deferral queue.

    Session composes one of these in its ``__init__``; every
    subsystem that needs to push (plan-mode approval, output-style
    swap, permission-mode change, …) calls into the instance
    methods instead of poking module-level state.

    Test fixtures that stub Session via ``Session.__new__`` MUST
    also set ``session.broadcast_bus = BroadcastBus()`` — the
    bus never returns a no-op default when unset; callers get
    ``AttributeError`` and the missing wire-up surfaces at test
    time.
    """

    def __init__(self) -> None:
        self._callbacks: list[BroadcastCallback] = []
        self._pending: list[BroadcastEvent] = []

    # ── Callback registration ──────────────────────────────────

    def register(self, callback: BroadcastCallback) -> None:
        """Append ``callback(channel, payload)`` to the callback
        list. Idempotent — registering the same callable twice
        does not double-fire (load-bearing for
        ``test_session::test_register_is_idempotent_on_same_callback``:
        both ``/plan`` and ``/accept`` slash-command bootstrap
        can register the same transport shim without inflating
        fan-out)."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    # ── Read-only accessors ────────────────────────────────────
    # Public reads for state callers used to inspect on the raw
    # lists — kept as accessors so the private list identity
    # stays owned by the bus.

    @property
    def has_callbacks(self) -> bool:
        """True when at least one callback is registered."""
        return bool(self._callbacks)

    @property
    def pending_count(self) -> int:
        """Number of events queued for post-run drain."""
        return len(self._pending)

    def callbacks_snapshot(self) -> list[BroadcastCallback]:
        """Copy of the callback list — safe to iterate without
        mutating the live subscription set."""
        return list(self._callbacks)

    # ── Immediate delivery ─────────────────────────────────────

    def emit(self, event: BroadcastEvent) -> None:
        """Fire every registered callback with the event's
        ``(channel, payload)``. Synchronous — callbacks enqueue,
        they don't await.

        Callbacks receive the payload dict by identity (no
        defensive copy); emitters that mutate their dict after
        firing see the change through the callback too.

        The callback list is iterated via ``list(self._callbacks)``
        so a callback that ``register()``s during broadcast joins
        the NEXT emit, not the current one — mutating a list
        during iteration would otherwise raise RuntimeError.
        """
        for cb in list(self._callbacks):
            try:
                cb(event.channel, event.payload)
            except Exception as exc:
                logger.debug(
                    "broadcast callback raised on channel %s: %s",
                    event.channel,
                    exc,
                )

    # ── Deferred delivery ──────────────────────────────────────

    def queue_post_run(self, event: BroadcastEvent) -> None:
        """Enqueue ``event`` for delivery after the current run
        wraps up.

        Used by tools whose result is meant to render *after* all
        the agent's content — most notably ``exit_plan_mode``. If
        we broadcast inline, the PlanCard appears mid-stream,
        above whatever closing message the agent emits.
        """
        self._pending.append(event)

    def drain_post_run(self, run_id: str | None = None) -> None:
        """Fire every queued event through :meth:`emit`, then
        clear the queue.

        Called by the BE stream consumer right after
        ``RunCompleted`` flushes. Safe on an empty queue (no-op).

        ``run_id`` (when supplied) is stamped onto every payload
        that doesn't already carry one — how ``plan_submitted``
        payloads acquire the run_id the FE needs for
        ``approve_plan`` / ``dismiss_plan``. The plan tool can't
        see the current run_id from inside its toolkit context,
        so the run-loop injects it at drain time. Stamping is
        delegated to :meth:`BroadcastEvent.with_run_id` so the
        "skip if already stamped" policy lives on the event
        model, not inline here.
        """
        if not self._pending:
            return
        # Snapshot + clear so a callback that re-queues doesn't
        # loop in the same drain pass — the re-queued entry
        # survives to the NEXT drain.
        pending = list(self._pending)
        self._pending.clear()
        for event in pending:
            self.emit(event.with_run_id(run_id))
