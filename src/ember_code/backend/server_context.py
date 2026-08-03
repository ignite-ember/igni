"""Conversation-context management RPCs.

Thin coordinator over :class:`Session` + :class:`PendingMessageStore`
that fronts six RPCs the FE calls on the status / compaction /
history-truncation surface. Wire schemas
(:class:`TruncateHistoryResult`, :class:`PendingMessage`) live in
the sibling :mod:`schemas_context` module — same pattern as
:mod:`server_codeindex` + :mod:`schemas_codeindex_rpc`.

* :meth:`ContextController.get_status` — status-bar snapshot.
* :meth:`ContextController.count_context_tokens` — locally count
  tokens of the current conversation.
* :meth:`ContextController.compact_if_needed` — compaction on
  threshold cross.
* :meth:`ContextController.extract_learnings` — fire-and-forget
  learning-pipeline push (delegates to :meth:`Session.extract_learnings`).
* :meth:`ContextController.truncate_history` — drop a run and
  every later run from the Agno session.
* :meth:`ContextController.get_pending_messages` — surface
  pre-persisted user messages that never completed a run.
* :meth:`ContextController.get_interrupted_runs` — durable
  interrupted-run records (cancel + error stamped). Drives the
  FE's banner UI for show / retry / discard / edit-prompt.
* :meth:`ContextController.discard_interrupted_run` — hard-delete
  one interrupted record; FE "Discard" action.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING

from ember_code.backend.schemas_context import (
    PENDING_STALENESS_SECONDS,
    DiscardInterruptedRunResult,
    InterruptedRun,
    PendingMessage,
    TruncateHistoryResult,
)
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.session import Session
    from ember_code.core.session.pending_messages import PendingMessageStore

logger = logging.getLogger(__name__)


class ContextController:
    """Context / status / compaction / learning / history-truncation
    controller for a single session."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        pending_store: PendingMessageStore,
    ) -> None:
        self._session = session
        self._settings = settings
        self._pending_store = pending_store

    def get_status(self) -> msg.StatusUpdate:
        """Status-bar snapshot. O(1) — reads the latched
        ``last_input_tokens`` counter."""
        return msg.StatusUpdate(
            model=self._settings.models.default,
            cloud_connected=self._session.cloud_connected,
            cloud_org=self._session.cloud_org_name or "",
            context_tokens=self._session.last_input_tokens,
            max_context=self._settings.models.max_context_window,
            permission_mode=self._session.permission_mode_value,
        )

    async def count_context_tokens(self) -> int:
        """Locally count tokens of the current conversation."""
        try:
            agno_session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
                user_id=self._session.user_id,
            )
        except Exception as exc:
            logger.debug("aget_session failed (%s); reporting 0", exc)
            return 0
        if agno_session is None:
            return 0
        try:
            messages = agno_session.get_messages()
        except Exception as exc:
            logger.debug("get_messages failed (%s); reporting 0", exc)
            return 0
        try:
            n = int(self._session.main_team.model.count_tokens(messages))
        except Exception as exc:
            logger.debug("count_tokens failed (%s); reporting 0", exc)
            return 0
        # Latch only when we actually measured something.
        if n > 0:
            self._session.latch_input_tokens(n)
        return n

    async def compact_if_needed(self, ctx_tokens: int, max_ctx: int) -> msg.SessionCleared | None:
        """Compact session if approaching context limit."""
        compacted = await self._session.compact_if_needed(ctx_tokens, max_ctx)
        if not compacted:
            return None
        summary = ""
        with contextlib.suppress(Exception):
            agno_session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
                user_id=self._session.user_id,
            )
            if agno_session and agno_session.summary and agno_session.summary.summary:
                summary = agno_session.summary.summary
        return msg.SessionCleared(
            new_session_id=self._session.session_id,
            summary=summary,
        )

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        """Push a completed turn into the learning pipeline.

        Thin forward to :meth:`Session.extract_learnings` — the
        None-guard on the learning machine and the background-task
        launch both live on :class:`Session`, so this coordinator
        stops reaching into ``session._learning``.
        """
        await self._session.extract_learnings(user_msg, assistant_msg)

    async def truncate_history(self, session_id: str, run_id: str) -> TruncateHistoryResult:
        """Drop the run with ``run_id`` and every later run from the
        session."""
        agent = self._session.main_team
        agno_session = await agent.aget_session(
            session_id=session_id,
            user_id=self._session.user_id,
        )
        if agno_session is None:
            return TruncateHistoryResult(removed=0, error="session not found")
        runs = list(getattr(agno_session, "runs", None) or [])
        cut_idx: int | None = None
        for i, r in enumerate(runs):
            if getattr(r, "parent_run_id", None):
                continue
            if str(getattr(r, "run_id", "") or "") == run_id:
                cut_idx = i
                break
        if cut_idx is None:
            return TruncateHistoryResult(removed=0, error=f"run_id {run_id!r} not in session")
        removed = len(runs) - cut_idx
        agno_session.runs = runs[:cut_idx]
        try:
            await agent.asave_session(agno_session)
        except Exception as exc:
            logger.exception("truncate_history: save failed")
            return TruncateHistoryResult(removed=0, error=str(exc))
        # The latched context-token count was computed against the
        # pre-truncate session; invalidate it so the next status
        # read recomputes from the new (shorter) history.
        self._session.latch_input_tokens(0)
        return TruncateHistoryResult(removed=removed)

    async def get_pending_messages(self, session_id: str) -> list[PendingMessage]:
        """Pending user messages that never completed a run.

        Oldest-first order. Only rows older than the staleness
        threshold are surfaced — a fresh row is almost always
        "Agno is still finishing its post-stream tail" and not a
        real interruption.
        """
        try:
            rows = await self._pending_store.alist_pending(session_id)
        except Exception as exc:
            logger.debug("get_pending_messages failed: %s", exc)
            return []
        cutoff = int(time.time()) - PENDING_STALENESS_SECONDS
        rows = [r for r in rows if r.received_at <= cutoff]
        return [PendingMessage.from_pending_row(r) for r in rows]

    async def get_interrupted_runs(self, session_id: str) -> list[InterruptedRun]:
        """Durable interrupted-run records for the session.

        Distinct from :meth:`get_pending_messages` in three ways:

        1. Returns ONLY rows explicitly stamped by the cancel +
           error paths in :class:`RunController`. A row that's
           ``pending`` but never stamped (very old data, or the
           stamp write was lost on crash) is invisible here.
        2. No staleness cutoff — an interrupted row that just
           landed is the most useful one to surface (the user just
           hit Esc and is staring at the spinner).
        3. Carries ``reason`` + ``last_error`` so the FE can
           render the right banner label ("Run cancelled" vs
           "Run stopped — model timeout") and surface the error
           text on hover.

        Returns wire models via
        :meth:`InterruptedRun.from_interrupted_row` so the
        controller stays out of the field-mapping business.
        """
        try:
            rows = await self._pending_store.alist_interrupted(session_id)
        except Exception as exc:
            logger.debug("get_interrupted_runs failed: %s", exc)
            return []
        return [InterruptedRun.from_interrupted_row(r) for r in rows]

    async def discard_interrupted_run(
        self, session_id: str, message_id: str
    ) -> DiscardInterruptedRunResult:
        """Hard-delete one interrupted run — the FE's "Discard" action.

        Idempotent: deleting a row that no longer exists returns
        ``ok=True`` so the FE can stop polling / re-trying. The
        row is matched by ``message_id`` (the wire-level id we
        exposed via :meth:`get_interrupted_runs`).

        Does NOT touch other interrupted rows for the session —
        only the one the user picked. The "discard all" semantics
        are reserved for the ``user_message(force=true)`` retry
        path (see :meth:`PendingMessageStore.discard_all_for_session`).
        """
        if not message_id:
            return DiscardInterruptedRunResult(ok=False, error="message_id is required")
        try:
            await self._pending_store.adiscard(message_id)
        except Exception as exc:
            logger.warning("discard_interrupted_run failed: %s", exc)
            return DiscardInterruptedRunResult(ok=False, error=str(exc))
        return DiscardInterruptedRunResult(ok=True)
