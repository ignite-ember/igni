"""Plan-decisions store — persists ``{run_id: decision}`` map.

Composes :class:`SessionDataService` for both the read
(``read_key('plan_decisions', PlanDecisionsBlob.from_raw)``) and
the write (``write_key('plan_decisions', blob.decisions_dict)``)
paths.

Rule 1 fix: the store's ``save`` takes ONLY the typed
:class:`PlanDecisionsBlob`; the legacy ``dict[str, str]`` branch
that used to live in ``SessionPersistence.save_plan_decisions``
is gone. The :class:`SessionPersistence` facade converts raw dicts
to a :class:`PlanDecisionsBlob` at its own boundary so legacy
callers (tests, backend flows) keep working without change.
"""

from __future__ import annotations

from ember_code.core.session.persistence.data_service import SessionDataService
from ember_code.core.session.schemas import LoadResult, PersistResult
from ember_code.core.tools.plan import PlanDecisionsBlob


class PlanDecisionsStore:
    """Coordinator for the ``plan_decisions`` sub-blob of
    ``session_data``."""

    KEY = "plan_decisions"

    def __init__(self, data_service: SessionDataService) -> None:
        self._data = data_service

    async def load(self) -> LoadResult[PlanDecisionsBlob]:
        """Read the persisted ``{run_id: decision}`` map.

        Returns :class:`LoadResult` — ``value`` is the parsed
        :class:`PlanDecisionsBlob` on hit, ``None`` on miss. A
        corrupt blob passes through :meth:`PlanDecisionsBlob.from_raw`
        which filters invalid entries — the store treats absence as
        "pending", the safe default.
        """
        return await self._data.read_key(self.KEY, PlanDecisionsBlob.from_raw)

    async def save(self, blob: PlanDecisionsBlob) -> PersistResult:
        """Write the typed :class:`PlanDecisionsBlob` to
        ``session_data['plan_decisions']``.

        ``model_dump(mode="json")`` collapses :class:`PlanDecision`
        StrEnum values to their ``.value`` strings so the on-disk
        shape stays a plain ``dict[str, str]`` — the same wire
        shape :meth:`load` reads back.
        """
        cleaned = blob.model_dump(mode="json")["decisions"]
        return await self._data.write_key(self.KEY, cleaned)
