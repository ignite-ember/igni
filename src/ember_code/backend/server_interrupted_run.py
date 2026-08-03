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

Detection sources (tried in order, first non-empty wins):

1. Explicit interrupted rows from the pending-message store —
   ``alist_interrupted`` returns rows that the cancel + error paths
   in :class:`RunController` stamped with ``interrupted_at``. This
   is the durable, queryable record added for the show/discard/
   retry FE UX — preferred over inference.
2. Agno ``RunStatus.running`` heuristic on the last run — legacy
   fallback for sessions whose interrupted row was never stamped
   (predates this change, or the mark_interrupted write was lost).
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
    from ember_code.core.session.pending_messages import (
        InterruptedMessage,
        PendingMessageStore,
    )

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

        Prefers the explicit ``alist_interrupted`` record over the
        legacy Agno ``RunStatus.running`` heuristic. Falls back to
        ``alist_pending`` + heuristic for legacy data — older DBs
        (predates this change, or the stamp write was lost on a
        hard crash) still have rows whose interruption is only
        inferable from "pending + agno session missing". Both
        probes are safe to run; the explicit-row probe simply
        short-circuits the heuristic when it hits.
        """
        try:
            interrupted = await self._probe_interrupted_rows()
            snapshot: AgnoRunSnapshot | None = None
            legacy_pending: list = []

            if not interrupted:
                # Legacy fallback — see module docstring. Without
                # this, an old DB that never had a row stamped
                # (predates this change) would silently lose its
                # interrupted-run detection.
                snapshot = await self._probe_agno_session()
                legacy_pending = await self._probe_pending_rows()
                if snapshot is None and not legacy_pending:
                    return None  # nothing to recover from — clean shutdown

            parts = ["Previous run was interrupted before completion."]
            if interrupted:
                # Use the explicit record as the source of truth.
                # The first row carries the user's text + reason;
                # any extras are appended for context.
                if len(interrupted) == 1:
                    parts.append(f"The user had asked: {interrupted[0].text!r}.")
                else:
                    qs = "; ".join(p.text for p in interrupted)
                    parts.append(f"The user had pending question(s): {qs!r}.")
                reasons = {p.interrupted_reason for p in interrupted}
                if "errored" in reasons:
                    err = next(
                        (p.last_error for p in interrupted if p.last_error),
                        None,
                    )
                    if err:
                        parts.append(f"Reason: {err!r}.")
                drop_ids = [p.message_id for p in interrupted]
            else:
                # Legacy path — drop ids are the pending rows the
                # agent hasn't seen yet. These get drained on the
                # next run_message so they don't keep resurfacing.
                drop_ids = [p.message_id for p in legacy_pending]
                if legacy_pending:
                    if len(legacy_pending) == 1:
                        parts.append(f"The user had asked: {legacy_pending[0].text!r}.")
                    else:
                        qs = "; ".join(p.text for p in legacy_pending)
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
                pending_ids_to_drop=drop_ids,
            )

            logger.info(
                "detected interrupted previous run "
                "(agno_run=%s, explicit_rows=%d, legacy_pending=%d); "
                "summary will be injected on next user message",
                snapshot.run_id if snapshot is not None else None,
                len(interrupted),
                len(legacy_pending),
            )
            return summary
        except Exception as exc:
            logger.debug("interrupted-run detection failed: %s", exc)
            return None

    async def _probe_agno_session(self) -> AgnoRunSnapshot | None:
        """Probe Agno's session for a ``RunStatus.running`` last-run.

        Legacy fallback for sessions whose interrupted row was never
        stamped (predates this change, or the mark was lost). Kept
        intact so an older DB still detects interrupted runs until
        it picks up the new schema on next ``_init_schema``.

        Returns ``None`` when the explicit-row probe already found
        a hit — avoids duplicate summary text.
        """
        explicit = await self._probe_interrupted_rows()
        if explicit:
            return None
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

    async def _probe_interrupted_rows(self) -> list[InterruptedMessage]:
        """Fetch the explicitly-stamped interrupted rows for this session.

        Empty list on any failure (including a missing store —
        ``__new__``-bypass test fixtures may not wire one). Used
        by both the summary builder and the Agno-probe fallback.
        """
        if self._pending_store is None:
            return []
        try:
            return await self._pending_store.alist_interrupted(self._session.session_id)
        except Exception:
            return []

    async def _probe_pending_rows(self) -> list:
        """Fetch pending-but-not-interrupted rows for the legacy path.

        Empty list on any failure (including a missing store).
        Pre-this-change DBs may have rows that are ``pending`` but
        never stamped ``interrupted_at`` — those still need to
        surface an interrupted summary so the agent can recover.
        """
        if self._pending_store is None:
            return []
        try:
            return await self._pending_store.alist_pending(self._session.session_id)
        except Exception:
            return []
