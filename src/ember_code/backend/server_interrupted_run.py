"""Home for :class:`InterruptedRunSummaryBuilder`.

Extracted from :mod:`ember_code.backend.server_lifecycle` — the
previous module held two duplicated 50-line summary-assembly blocks
(one on ``LifecycleController.detect_interrupted_run``, one on the
free-function shim for the ``__new__``-bypass test path). Both
blocks probed Agno's ``session.runs`` for a ``RunStatus.running``
last-run, queried the pending-message store, and assembled the
same ``<system-context>`` prose.

This module puts the whole thing behind one class. Consumers hold
a builder and call :meth:`build` — no attribute-probing on Agno's
dynamic shapes at any callsite outside this file.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.run.base import RunStatus

from ember_code.backend.schemas_lifecycle import (
    AgnoRunSnapshot,
    InterruptedRunSummary,
)

if TYPE_CHECKING:
    from ember_code.core.session import Session
    from ember_code.core.session.pending_messages import PendingMessageStore

logger = logging.getLogger(__name__)


class InterruptedRunSummaryBuilder:
    """Assemble a :class:`InterruptedRunSummary` from an Agno session
    + pending-message store.

    One instance per :class:`LifecycleController`. Owns all the
    getattr-defensive Agno probing (via :class:`AgnoRunSnapshot`) +
    the pending-store query + the prose assembly.
    """

    def __init__(
        self,
        session: Session,
        pending_store: PendingMessageStore | None,
    ) -> None:
        self._session = session
        self._pending_store = pending_store

    async def build(self) -> InterruptedRunSummary | None:
        """Return a typed summary if the previous run was interrupted,
        else ``None`` when the previous shutdown was clean.

        Best-effort throughout — every failure path returns
        ``None`` rather than raising, since a broken interrupted-
        run detection must not block startup.
        """
        try:
            snapshot = await self._probe_agno_session()
            pending = await self._probe_pending_messages()

            if snapshot is None and not pending:
                return None  # nothing to recover from — clean shutdown

            parts = ["Previous run was interrupted before completion."]
            if pending:
                if len(pending) == 1:
                    parts.append(f"The user had asked: {pending[0].text!r}.")
                else:
                    qs = "; ".join(p.text for p in pending)
                    parts.append(f"The user had pending question(s): {qs!r}.")

            if snapshot is not None:
                if snapshot.tool_names:
                    parts.append(f"Tool calls completed: {', '.join(snapshot.tool_names)}.")
                if snapshot.content_preview.strip():
                    parts.append(f"Partial response so far: {snapshot.content_preview!r}.")

            parts.append(
                "The user has not yet sent a new message. Decide whether to "
                "continue, recap what you found, or ask for direction."
            )
            summary = InterruptedRunSummary(
                summary_text=" ".join(parts),
                pending_ids_to_drop=[p.message_id for p in pending],
            )

            logger.info(
                "detected interrupted previous run "
                "(agno_run=%s, pending=%d); summary will be injected on next user message",
                snapshot.run_id if snapshot is not None else None,
                len(pending),
            )
            return summary
        except Exception as exc:
            logger.debug("interrupted-run detection failed: %s", exc)
            return None

    async def _probe_agno_session(self) -> AgnoRunSnapshot | None:
        """Probe Agno's session for a ``RunStatus.running`` last-run.

        Single site for all Agno-side attribute defence. Returns
        the typed snapshot when the previous run was interrupted,
        else ``None``.
        """
        try:
            agno_session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
            )
        except Exception as exc:
            logger.debug("interrupted-run: aget_session failed: %s", exc)
            return None
        if agno_session is None:
            return None
        runs = getattr(agno_session, "runs", None) or []
        if not runs:
            return None
        last = runs[-1]
        if getattr(last, "status", None) != RunStatus.running:
            return None
        return AgnoRunSnapshot.from_agno_run(last)

    async def _probe_pending_messages(self) -> list:
        """Fetch the pending-message rows for this session.

        Empty list on any failure (including a missing store —
        ``__new__``-bypass test fixtures may not wire one).
        """
        if self._pending_store is None:
            return []
        try:
            return await self._pending_store.alist_pending(self._session.session_id)
        except Exception:
            return []
