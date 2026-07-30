"""Event-log store — persists the per-session append-only log.

Composes :class:`SessionDataService`. Load parses each persisted
row via :meth:`SessionEvent.from_wire` and filters ``None``s so
stale / schema-drifted rows drop silently instead of surfacing
as a ``KeyError`` deep in the splicer. Save dumps at the SQL
boundary.

The wire semantics match the pre-refactor
``SessionPersistence.load_event_log`` / ``save_event_log`` pair
byte-for-byte: the persisted shape stays
``list[dict]`` (Agno's ``session_data`` column round-trips JSON
without a Pydantic step), and every returned entry rolls through
one :class:`SessionEvent` definition.
"""

from __future__ import annotations

from ember_code.core.session.event_log_schema import SessionEvent
from ember_code.core.session.persistence.data_service import SessionDataService
from ember_code.core.session.schemas import LoadResult, PersistResult


class EventLogStore:
    """Coordinator for the ``event_log`` sub-blob of ``session_data``."""

    KEY = "event_log"

    def __init__(self, data_service: SessionDataService) -> None:
        self._data = data_service

    async def load(self) -> LoadResult[list[SessionEvent]]:
        """Read the persisted event log as typed :class:`SessionEvent`
        rows.

        Every persisted entry is round-tripped through
        :meth:`SessionEvent.from_wire` before being surfaced —
        stale / schema-drifted rows drop silently, and the output
        list comes from one Pydantic definition (Rule 1).
        """
        return await self._data.read_key(self.KEY, self._parse)

    async def save(self, events: list[SessionEvent]) -> PersistResult:
        """Atomic-replace the persisted event log.

        Callers pass typed :class:`SessionEvent` instances; the
        store dumps each to a wire dict at the SQL boundary so the
        domain stays typed and the wire-dump happens exactly
        once, inside the store (Rule 1).
        """
        wire = [e.model_dump() for e in events]
        return await self._data.write_key(self.KEY, wire)

    @staticmethod
    def _parse(raw: object) -> list[SessionEvent]:
        """Coerce a persisted ``list[dict]`` back to
        ``list[SessionEvent]``. Malformed entries drop silently."""
        if not isinstance(raw, list):
            return []
        out: list[SessionEvent] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            evt = SessionEvent.from_wire(entry)
            if evt is not None:
                out.append(evt)
        return out
