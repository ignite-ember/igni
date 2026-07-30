"""Plan / todo snapshot builder.

Extracted from :class:`BackendServer`. The previous inline methods
``get_latest_plan`` (25 LoC) and ``get_todos`` (15 LoC) each
reached into two session-owned stores (``plan_store``,
``todo_store``) with defensive ``getattr`` walls. Consolidating
into a dedicated class:

* Owns the ``session`` reference as a constructor arg.
* Returns typed :class:`LatestPlanResult` (schemas_plan) and a
  ``list[dict]`` matching the pre-refactor snapshot format.
* Keeps ``getattr(session, "plan_store", None)`` guards in one
  place because tests set the session to a ``MagicMock`` that
  spawns undeclared attrs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.backend.schemas_plan import LatestPlanResult
from ember_code.core.tools.todo import TodoItemWire

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class PlanSnapshotBuilder:
    """Read-only view of :attr:`Session.plan_store` +
    :attr:`Session.todo_store` for the plan-mode panel and the
    todos panel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest(self) -> LatestPlanResult:
        """Snapshot of the plan store + todos + display state for
        the FE panel.

        ``state`` is ``"pending"`` when a plan exists (the FE
        proves otherwise via ``approve_plan`` / ``dismiss_plan``)
        and empty when no plan has been submitted yet. Never
        inferred from permission mode — a mode flip without an
        explicit user click leaves the plan pending.
        """
        store = getattr(self._session, "plan_store", None)
        todo_store = getattr(self._session, "todo_store", None)
        if store is None:
            return LatestPlanResult()
        snap = store.snapshot()
        latest = snap.latest or ""
        raw_tasks: list[dict] = []
        if todo_store is not None:
            try:
                raw_tasks = todo_store.snapshot()
            except Exception as exc:
                logger.debug("plan snapshot: todo snapshot failed: %s", exc)
        return LatestPlanResult(
            latest=latest,
            history=list(snap.history),
            tasks=TodoItemWire.coerce_snapshot(raw_tasks),
            state="pending" if latest else "",
        )

    def todos(self) -> list[dict]:
        """Snapshot of the session's todo list for the todos panel.

        Returns whatever the last ``todo_write`` tool call
        published (in ``activeForm``-camelCase shape, matching the
        SDK payload). Empty list when the store is missing (legacy
        serialised session) or was never written.
        """
        store = getattr(self._session, "todo_store", None)
        if store is None:
            return []
        try:
            return store.snapshot()
        except Exception as exc:
            logger.debug("todos snapshot failed: %s", exc)
            return []
