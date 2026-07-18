"""Compaction coordinator — orchestrates the auto + manual paths.

Extracted from :mod:`ember_code.core.session.core` — the code
that summarises the conversation and drops old runs to free
context. Auto-compaction fires at 80% context usage;
:meth:`CompactionCoordinator.force_compact` triggers the manual
``/compact`` path; :meth:`CompactionCoordinator.context_breakdown`
is delegated to :class:`ContextBreakdownReporter`.

The coordinator composes two collaborators:

* :class:`FallbackSummariser` — runs a free-text summariser
  when Agno's structured :class:`SessionSummaryManager` returns
  empty (MiniMax-M2.7 workaround; see the summariser's own
  module docstring for the full story).
* :class:`ContextBreakdownReporter` — owns the ``/ctx`` token
  accounting.

Both public entry points return a
:class:`~ember_code.core.session.schemas.CompactResult`
envelope so callers stop tuple-unpacking or ``str | None``-
checking at every call site.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.session.summary import SessionSummary, SessionSummaryManager

from ember_code.core.hooks.events import HookEvent
from ember_code.core.session.compaction.context_breakdown_reporter import (
    ContextBreakdownReporter,
)
from ember_code.core.session.compaction.fallback_summariser import FallbackSummariser
from ember_code.core.session.schemas import (
    CompactResult,
    ContextBreakdown,
    PostCompactHookPayload,
    PreCompactHookPayload,
)

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class CompactionCoordinator:
    """Owns compaction (auto + manual) plus the context-breakdown
    reporter.

    Constructor holds a reference to the session so it can read
    the live ``main_team`` (reassigned by :meth:`compact`,
    plugin-reload, MCP-rebuild) at call time. The compaction
    path itself calls :meth:`Session.rebuild_main_team` —
    rebuilding from scratch is the only reliable way to clear
    Agno's in-memory message cache.
    """

    _COMPACT_THRESHOLD = 0.8

    def __init__(self, session: Session) -> None:
        self._session = session
        self._fallback = FallbackSummariser(session)
        self._reporter = ContextBreakdownReporter(session)

    async def compact(self) -> CompactResult:
        """Generate a summary of the conversation, then clear old runs.

        1. Generate summary covering the full conversation via
           Agno's structured summariser.
        2. If that returns empty, fall back to the free-text
           summariser (MiniMax workaround — see
           :class:`FallbackSummariser`).
        3. Delete all runs from the session (summary preserved).
        4. Rebuild the main agent so its in-memory message cache
           is empty and the next turn sees the summary as system
           context.

        Returns a :class:`CompactResult` — ``ok`` is ``True`` when
        a non-empty summary landed; ``error`` carries the
        summariser exception when the pass failed.
        """
        session = self._session
        agno_session = await session.main_team.aget_session(
            session_id=session.session_id,
            user_id=session.user_id,
        )
        if agno_session is None:
            logger.warning("No session found to compact")
            return CompactResult(
                ok=False,
                status="Session not found in DB",
                error="Session not found in DB",
            )

        summariser_error: str | None = None
        try:
            ssm = SessionSummaryManager(model=session.main_team.model)
            await ssm.acreate_session_summary(session=agno_session)
            logger.info("Session summary generated")
        except Exception as e:
            # Surface the underlying error so the UI can explain why the
            # summary is empty instead of just showing a generic placeholder.
            summariser_error = f"{type(e).__name__}: {e}"
            logger.warning("Failed to generate session summary: %s", e)

        summary_obj = getattr(agno_session, "summary", None)
        summary_text = getattr(summary_obj, "summary", None) if summary_obj else None
        if not summary_text:
            try:
                summary_text = await self._fallback.summarise(agno_session)
            except Exception as e:
                logger.warning("Fallback summariser failed: %s", e)
                if not summariser_error:
                    summariser_error = f"fallback: {type(e).__name__}: {e}"
            else:
                if summary_text:
                    try:
                        agno_session.summary = SessionSummary(summary=summary_text)
                        logger.info(
                            "Session summary generated via fallback (%d chars)",
                            len(summary_text),
                        )
                    except Exception as e:
                        logger.warning("Could not attach fallback summary to session: %s", e)

        agno_session.runs = []
        try:
            await session.main_team.asave_session(agno_session)
            logger.info("Session runs cleared from DB")
        except Exception as e:
            logger.warning("Failed to save session: %s", e)

        # Rebuild main agent through the public seam.
        session.rebuild_main_team()
        logger.info("Compacted: summary injected, agent rebuilt")

        return CompactResult(
            ok=summariser_error is None and bool(summary_text),
            status=summariser_error or "",
            summary=summary_text or "",
            error=summariser_error,
        )

    async def compact_if_needed(self, input_tokens: int, context_window: int) -> bool:
        """Auto-compact at 80% context usage.

        Messages accumulate freely until context fills up. At 80%, a
        summary is generated and old turns are dropped. Returns
        ``True`` if compaction was applied.
        """
        session = self._session
        if context_window <= 0 or input_tokens <= 0:
            return False

        usage = input_tokens / context_window
        if usage < self._COMPACT_THRESHOLD:
            return False

        # PreCompact hook — lets plugins export / summarise / cancel
        # before history is dropped. A blocking return cancels the
        # compaction (the auto trigger respects it; the user can
        # always retry manually).
        pre_payload = PreCompactHookPayload(
            session_id=session.session_id,
            scope="auto",
            tokens_before=input_tokens,
        )
        pre = await session.hook_executor.execute(
            event=HookEvent.PRE_COMPACT.value,
            payload=pre_payload.model_dump(),
        )
        if not pre.should_continue:
            logger.info("Auto-compact cancelled by PreCompact hook: %s", pre.message)
            return False

        await self.compact()
        logger.info("Auto-compacted at %.0f%% context usage", usage * 100)
        # PostCompact hook — observation only; can't undo at this point.
        post_payload = PostCompactHookPayload(
            session_id=session.session_id,
            scope="auto",
            tokens_before=input_tokens,
        )
        await session.hook_executor.execute(
            event=HookEvent.POST_COMPACT.value,
            payload=post_payload.model_dump(),
        )
        return True

    async def force_compact(self) -> CompactResult:
        """Manually compact conversation context.

        Returns a :class:`CompactResult` — ``status`` carries the
        human-readable status line for the ``/compact`` card,
        ``summary`` carries the generated summary text (empty on
        the failure branches), and ``error`` carries the
        underlying summariser exception when the pass failed.
        """
        session = self._session
        # Check if there's anything to compact.
        try:
            agno_session = await session.main_team.aget_session(
                session_id=session.session_id,
                user_id=session.user_id,
            )
            if agno_session is None or not agno_session.runs:
                return CompactResult(
                    ok=False,
                    status="Nothing to compact — no conversation history.",
                    summary="",
                )
        except Exception as exc:
            logger.warning("force_compact pre-check aget_session failed: %s", exc)

        # PreCompact hook (manual /compact path). Honouring the blocking
        # decision: the user invoked the command but a plugin can still
        # veto (e.g. an unsaved-changes guard).
        pre_payload = PreCompactHookPayload(
            session_id=session.session_id,
            scope="manual",
            tokens_before=0,
        )
        pre = await session.hook_executor.execute(
            event=HookEvent.PRE_COMPACT.value,
            payload=pre_payload.model_dump(),
        )
        if not pre.should_continue:
            return CompactResult(
                ok=False,
                status=pre.message or "Compaction cancelled by PreCompact hook.",
                summary="",
            )

        inner = await self.compact()
        error = inner.error

        # Retrieve the generated summary from DB.
        summary = ""
        try:
            agno_session = await session.main_team.aget_session(
                session_id=session.session_id,
                user_id=session.user_id,
            )
            if agno_session and agno_session.summary:
                summary = agno_session.summary.summary or ""
        except Exception as exc:
            logger.warning("force_compact post-fetch aget_session failed: %s", exc)

        if error and not summary:
            status = f"Context cleared, but the summariser failed: {error}"
            ok = False
        elif not summary:
            status = (
                "Context cleared, but the summariser returned no text "
                "(MiniMax may have returned an unparseable response)."
            )
            ok = False
        else:
            status = "Context compacted. Conversation summarized, history cleared."
            ok = True
        # PostCompact fires whether the summariser succeeded or not —
        # observers still need the history-cleared signal.
        post_payload = PostCompactHookPayload(
            session_id=session.session_id,
            scope="manual",
            tokens_before=0,
            summary_chars=len(summary),
        )
        await session.hook_executor.execute(
            event=HookEvent.POST_COMPACT.value,
            payload=post_payload.model_dump(),
        )
        return CompactResult(ok=ok, status=status, summary=summary, error=error)

    async def context_breakdown(self) -> ContextBreakdown:
        """Delegate to :meth:`ContextBreakdownReporter.report`."""
        return await self._reporter.report()
