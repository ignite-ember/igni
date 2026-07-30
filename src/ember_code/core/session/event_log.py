"""Append-only event log coordinator for :class:`Session`.

Extracted from :mod:`ember_code.core.session.core` — the
``event_log`` / ``_event_seq`` / ``append_event`` /
``restore_event_log`` orbit graduates to a dedicated class here.

Owns the "seq monotonic within a session" invariant: every append
increments the counter before the event is built, so persisted
rows can be reordered by ``seq`` without wall-clock skew concerns.

Persistence writes go through :class:`SessionPersistence` — the
coordinator receives a callable so it stays testable without a
DB (unit tests can inject a no-op persister).

Rule 6 (oop_offender #3): a coordinator class replaces the four
sprawled attributes / methods on the Session god-class.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ember_code.core.session.event_log_schema import SessionEvent

logger = logging.getLogger(__name__)


class SessionEventLog:
    """Owns the per-session append-only event log.

    Constructor takes a ``persist_ref`` callable that returns the
    current :class:`SessionPersistence` (may be ``None`` for
    bare-Session test stubs). Every :meth:`append` reads the
    current persister so a re-bind of ``session.persistence`` is
    honoured.
    """

    def __init__(
        self,
        persist_ref: Callable[[], Any] | None = None,
    ) -> None:
        self._events: list[SessionEvent] = []
        self._seq: int = 0
        self._persist_ref = persist_ref or (lambda: None)

    async def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        run_id: str = "",
    ) -> None:
        """Record ``(type, payload)`` on the log and persist.

        The counter is incremented BEFORE the event is built so a
        concurrent read of :attr:`events` mid-append never sees a
        row with ``seq=0``. Persistence failures log at DEBUG
        rather than propagate — the in-memory log still reaches
        live clients.
        """
        self._seq += 1
        event = SessionEvent.build(
            seq=self._seq,
            event_type=event_type,
            payload=payload,
            run_id=run_id,
        )
        self._events.append(event)
        persistence = self._persist_ref()
        if persistence is None:
            return
        try:
            # Pass the typed list directly — the persistence
            # facade dumps at its own SQL boundary (Rule 1: the
            # domain stays typed to the boundary, wire coercion
            # happens once, inside the store).
            await persistence.save_event_log(list(self._events))
        except Exception as exc:  # noqa: BLE001 — best-effort persistence
            logger.debug("event_log persist failed: %s", exc)

    def restore(self, events: list[SessionEvent]) -> None:
        """Replace the in-memory log AND its seq counter atomically.

        Wired by :meth:`RehydrateController.event_log` on startup
        so the counter stays in sync with the highest ``seq`` on
        disk — otherwise the next :meth:`append` would collide
        with a persisted row.
        """
        self._events = events
        self._seq = max((e.seq for e in events), default=0)

    @property
    def events(self) -> list[SessionEvent]:
        """Live reference to the in-memory event list.

        Kept as a live reference (not a snapshot) so callers that
        historically wrote to ``session.event_log`` — e.g. test
        fixtures that seed rows directly — keep working. New code
        should treat the return value as read-only.
        """
        return self._events

    @events.setter
    def events(self, value: list[SessionEvent]) -> None:
        """Compat setter — mirrors the legacy
        ``session.event_log = [...]`` fixture pattern. Rewires the
        seq counter so a subsequent :meth:`append` doesn't collide
        with the seeded rows.
        """
        self._events = list(value)
        self._seq = max((e.seq for e in self._events), default=0)

    @property
    def seq(self) -> int:
        """Current monotonic sequence counter."""
        return self._seq
