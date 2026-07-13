"""Session-level broadcast machinery.

Extracted from :mod:`ember_code.core.session.core` so the
push-channel fan-out has one owner. Each function takes the
session as an explicit argument and works against
``session._broadcast_callbacks`` (list of
``(channel, payload) -> None`` callables) and
``session._pending_post_run_broadcasts`` (list of
``(channel, payload)`` tuples deferred until a run finishes).

Delivery contract:

* :func:`broadcast` fires every callback synchronously with
  ``(channel, payload)``. Callbacks are expected to just enqueue
  a push, not to await IO. Exceptions in one callback don't sink
  the rest — each is guarded independently.
* :func:`queue_post_run_broadcast` defers delivery until the
  stream consumer calls :func:`drain_post_run_broadcasts` (right
  after ``RunCompleted`` / ``RunError``). Used by tools whose
  card must appear at the *bottom* of a reply, not mid-stream —
  most notably ``exit_plan_mode``'s PlanCard.

All functions defensively tolerate a partially-initialised
session (``Session.__new__`` bypass): missing lists degrade to
a no-op instead of raising.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


def register_broadcast_callback(
    session: "Session",
    callback: Callable[[str, dict], None],
) -> None:
    """Append a ``callback(channel, payload)`` to the session's
    broadcast list. Idempotent — the same callback registered
    twice doesn't double-fire."""
    if callback not in session._broadcast_callbacks:
        session._broadcast_callbacks.append(callback)


def broadcast(session: "Session", channel: str, payload: dict) -> None:
    """Fire every registered broadcast callback with
    ``(channel, payload)``. Synchronous — callbacks enqueue,
    they don't await."""
    callbacks = getattr(session, "_broadcast_callbacks", None)
    if not callbacks:
        return
    for cb in list(callbacks):
        try:
            cb(channel, payload)
        except Exception as exc:
            logger.debug("broadcast callback raised on channel %s: %s", channel, exc)


def queue_post_run_broadcast(session: "Session", channel: str, payload: dict) -> None:
    """Same delivery as :func:`broadcast` but deferred until the
    current run finishes streaming.

    Used by tools whose result is meant to render *after* all the
    agent's content — most notably ``exit_plan_mode``. If we
    broadcast inline, the PlanCard appears mid-stream, above
    whatever closing message the agent emits.

    Defensive against partially-initialised sessions: falls back
    to immediate broadcast when the queue attr is missing.
    """
    queue = getattr(session, "_pending_post_run_broadcasts", None)
    if queue is None:
        # Init missing (test path) → immediate so we don't silently
        # swallow the event.
        broadcast(session, channel, payload)
        return
    queue.append((channel, payload))


def drain_post_run_broadcasts(session: "Session", run_id: str | None = None) -> None:
    """Fire every queued post-run broadcast through :func:`broadcast`,
    then clear the queue.

    Called by the BE stream consumer right after ``RunCompleted``
    flushes. Safe on a clean session (no-op).

    ``run_id`` (when supplied) is stamped onto every payload that
    doesn't already carry one — how ``plan_submitted`` payloads
    acquire the run_id the FE needs for ``approve_plan`` /
    ``dismiss_plan``. The plan tool can't see the current run_id
    from inside its toolkit context, so the run-loop injects it
    at drain time.
    """
    queue = getattr(session, "_pending_post_run_broadcasts", None)
    if not queue:
        return
    # Snapshot + clear so a callback that re-queues doesn't loop in
    # the same drain pass.
    pending = list(queue)
    queue.clear()
    for channel, payload in pending:
        if run_id and isinstance(payload, dict) and "run_id" not in payload:
            payload = {**payload, "run_id": run_id}
        broadcast(session, channel, payload)
