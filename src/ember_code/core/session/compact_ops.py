"""Session compaction helpers.

Extracted from :mod:`ember_code.core.session.core` — the code
that summarises the conversation and drops old runs to free
context. Auto-compaction fires at 80% context; ``/compact``
triggers manual compaction; ``/ctx`` calls
:func:`context_breakdown` to explain the irreducible floor.

The two-step summariser design is a workaround for MiniMax-M2.7:
Agno's ``SessionSummaryManager`` uses a JSON-schema prompt that
the reasoning model often fails to fill in (returns without
raising but the summary stays empty). The fallback summariser
uses a plain free-text prompt.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from agno.models.message import Message as AgnoMessage
from agno.session.summary import SessionSummaryManager
from pydantic import BaseModel

from ember_code.core.hooks.events import HookEvent

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class ContextBreakdown(BaseModel):
    """Per-component token breakdown of the current context.

    ``total = runs + floor`` (``floor`` clamped to ``0`` in case
    of tokenizer inconsistency where ``runs`` > ``total``).
    Consumed by the ``/ctx`` slash command to explain the
    irreducible portion `/compact` cannot shrink."""

    total: int
    runs: int
    floor: int


async def compact(session: "Session") -> str | None:
    """Generate a summary of the conversation, then clear old messages.

    1. Generate summary covering the full conversation.
    2. Delete all runs from the session (summary preserved).
    3. Rebuild the main agent so its in-memory message cache is
       empty and the next turn sees the summary as system context.

    Returns an error message if the summariser failed (history is
    still cleared, but the summary will be empty). Returns
    ``None`` on success.
    """
    agno_session = await session.main_team.aget_session(
        session_id=session.session_id,
        user_id=session.user_id,
    )
    if agno_session is None:
        logger.warning("No session found to compact")
        return "Session not found in DB"

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

    # Fallback summariser: Agno's SessionSummaryManager hands the
    # model a JSON-structured prompt, which MiniMax-M2.7 often fails
    # to fill in — the call returns without raising but the summary
    # stays empty. Run a plain free-text summariser if that
    # happened, so the user gets a useful card instead of a
    # silently-empty one.
    summary_obj = getattr(agno_session, "summary", None)
    summary_text = getattr(summary_obj, "summary", None) if summary_obj else None
    if not summary_text:
        try:
            summary_text = await _fallback_summarise(session, agno_session)
        except Exception as e:
            logger.warning("Fallback summariser failed: %s", e)
            if not summariser_error:
                summariser_error = f"fallback: {type(e).__name__}: {e}"
        else:
            if summary_text:
                # Hand the text to Agno's summary structure so
                # subsequent runs see it via the normal injection
                # path. ``SessionSummary`` is what
                # SessionSummaryManager populates on success.
                try:
                    from agno.session.summary import SessionSummary

                    agno_session.summary = SessionSummary(summary=summary_text)
                    logger.info(
                        "Session summary generated via fallback (%d chars)",
                        len(summary_text),
                    )
                except Exception as e:
                    logger.warning("Could not attach fallback summary to session: %s", e)
                    # Keep ``summary_text`` non-empty so ``force_compact``
                    # can still surface it to the user even if
                    # persisting failed.

    # Clear runs — summary stays.
    agno_session.runs = []
    try:
        await session.main_team.asave_session(agno_session)
        logger.info("Session runs cleared from DB")
    except Exception as e:
        logger.warning("Failed to save session: %s", e)

    # Rebuild the main agent from scratch. This is the only reliable
    # way to clear Agno's in-memory message history — the cached
    # session, run_response, and internal state all hold old messages.
    session.main_team = session._build_main_agent()
    logger.info("Compacted: summary injected, agent rebuilt")
    return summariser_error


async def _fallback_summarise(session: "Session", agno_session: Any) -> str:
    """Plain-text summariser used when Agno's structured summariser
    returns nothing. Builds a short transcript from the session's
    runs, asks the model for a free-form summary, and strips any
    ``<think>…</think>`` blocks the reasoning model emits.
    """
    # Pull a compact transcript from the runs. ``input`` / output
    # are the only fields the summary cares about; tool calls and
    # internal events would just bloat the prompt and risk hitting
    # the context cap for a *summarise the context* call.
    lines: list[str] = []
    for run in getattr(agno_session, "runs", None) or []:
        try:
            msgs = getattr(run, "messages", None) or []
            for m in msgs:
                role = getattr(m, "role", None) or m.get("role")  # type: ignore[union-attr]
                content = getattr(m, "content", None) or (
                    m.get("content") if hasattr(m, "get") else None
                )
                if (
                    role in ("user", "assistant")
                    and isinstance(content, str)
                    and content.strip()
                ):
                    # Cap per-message length so a single long response
                    # doesn't dominate the prompt.
                    snippet = content.strip()
                    if len(snippet) > 800:
                        snippet = snippet[:800] + "…"
                    lines.append(f"{role.upper()}: {snippet}")
        except Exception:
            continue
    if not lines:
        return ""

    transcript = "\n\n".join(lines[-60:])  # most recent ~60 messages
    prompt = (
        "Summarise the following conversation in 4-8 sentences. "
        "Focus on what the user asked, what the assistant did, and any "
        "open threads. Output ONLY the summary text — no preamble, no "
        "JSON, no <think> tags, no markdown headers.\n\n"
        "--- CONVERSATION ---\n"
        f"{transcript}\n"
        "--- END ---"
    )

    # Use the main team's model directly. ``Model.aresponse`` returns
    # the raw model response; we extract the text and strip thinking
    # tags defensively (MiniMax leaks them through even when asked
    # not to).
    model = session.main_team.model
    try:
        resp = await model.aresponse(messages=[AgnoMessage(role="user", content=prompt)])
    except Exception as e:
        logger.warning("fallback aresponse failed: %s", e)
        return ""

    text = getattr(resp, "content", None) or ""
    if not isinstance(text, str):
        return ""
    # Strip <think>...</think> blocks — including unclosed ones at
    # end-of-string.
    text = re.sub(r"<think>[\s\S]*?(</think>\s*|$)", "", text).strip()
    return text


async def compact_if_needed(session: "Session", input_tokens: int, context_window: int) -> bool:
    """Auto-compact at 80% context usage.

    Messages accumulate freely until context fills up. At 80%, a
    summary is generated and old turns are dropped. Returns True
    if compaction was applied.
    """
    if context_window <= 0 or input_tokens <= 0:
        return False

    usage = input_tokens / context_window
    if usage < 0.8:
        return False

    # PreCompact hook — lets plugins export / summarise / cancel
    # before history is dropped. A blocking return cancels the
    # compaction (the auto trigger respects it; the user can
    # always retry manually).
    pre = await session.hook_executor.execute(
        event=HookEvent.PRE_COMPACT.value,
        payload={
            "session_id": session.session_id,
            "scope": "auto",
            "tokens_before": input_tokens,
        },
    )
    if not pre.should_continue:
        logger.info("Auto-compact cancelled by PreCompact hook: %s", pre.message)
        return False

    await compact(session)
    logger.info("Auto-compacted at %.0f%% context usage", usage * 100)
    # PostCompact hook — observation only; can't undo at this point.
    await session.hook_executor.execute(
        event=HookEvent.POST_COMPACT.value,
        payload={
            "session_id": session.session_id,
            "scope": "auto",
            "tokens_before": input_tokens,
        },
    )
    return True


async def force_compact(session: "Session") -> tuple[str, str]:
    """Manually compact conversation context.

    Returns ``(status_message, summary_text)``. When the summariser
    fails, the status carries the underlying error and the summary
    is the empty string.
    """
    # Check if there's anything to compact.
    try:
        agno_session = await session.main_team.aget_session(
            session_id=session.session_id,
            user_id=session.user_id,
        )
        if agno_session is None or not agno_session.runs:
            return "Nothing to compact — no conversation history.", ""
    except Exception:
        pass

    # PreCompact hook (manual /compact path). Honouring the blocking
    # decision: the user invoked the command but a plugin can still
    # veto (e.g. an unsaved-changes guard).
    pre = await session.hook_executor.execute(
        event=HookEvent.PRE_COMPACT.value,
        payload={
            "session_id": session.session_id,
            "scope": "manual",
            "tokens_before": 0,
        },
    )
    if not pre.should_continue:
        return (pre.message or "Compaction cancelled by PreCompact hook.", "")

    error = await compact(session)

    # Retrieve the generated summary from DB.
    summary = ""
    try:
        agno_session = await session.main_team.aget_session(
            session_id=session.session_id,
            user_id=session.user_id,
        )
        if agno_session and agno_session.summary:
            summary = agno_session.summary.summary or ""
    except Exception:
        pass

    if error and not summary:
        status = f"Context cleared, but the summariser failed: {error}"
    elif not summary:
        status = (
            "Context cleared, but the summariser returned no text "
            "(MiniMax may have returned an unparseable response)."
        )
    else:
        status = "Context compacted. Conversation summarized, history cleared."
    # PostCompact hook — fired regardless of summariser success so
    # observers can still react to the history-cleared half of the
    # operation. ``summary_chars`` is 0 in the failure path.
    await session.hook_executor.execute(
        event=HookEvent.POST_COMPACT.value,
        payload={
            "session_id": session.session_id,
            "scope": "manual",
            "tokens_before": 0,
            "summary_chars": len(summary),
        },
    )
    return status, summary


async def context_breakdown(session: "Session") -> ContextBreakdown:
    """Return per-component token counts for the current context.

    Splits Agno's assembled message list into:

    * ``runs``  — conversational turns (user/assistant/tool
      messages held under ``agno_session.runs``)
    * ``floor`` — everything else baked into every prompt: system
      instructions, tool schemas, project rules, active memories,
      injected session summary
    * ``total`` — the full ``count_context_tokens`` figure
      (== runs + floor)

    Used by ``/ctx`` to explain the irreducible floor that
    ``/compact`` cannot shrink.
    """
    try:
        agno_session = await session.main_team.aget_session(
            session_id=session.session_id,
            user_id=session.user_id,
        )
    except Exception:
        return ContextBreakdown(total=0, runs=0, floor=0)
    if agno_session is None:
        return ContextBreakdown(total=0, runs=0, floor=0)

    try:
        all_messages = agno_session.get_messages()
        total = int(session.main_team.model.count_tokens(all_messages))
    except Exception:
        total = 0

    run_messages: list = []
    for run in getattr(agno_session, "runs", None) or []:
        msgs = getattr(run, "messages", None)
        if msgs:
            run_messages.extend(msgs)
    try:
        runs_tokens = (
            int(session.main_team.model.count_tokens(run_messages)) if run_messages else 0
        )
    except Exception:
        runs_tokens = 0

    floor = max(0, total - runs_tokens)
    return ContextBreakdown(total=total, runs=runs_tokens, floor=floor)
