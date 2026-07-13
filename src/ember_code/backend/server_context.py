"""Conversation-context management RPCs.

Extracted from :mod:`ember_code.backend.server`. Six free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates. All operations are about "how much
conversation is in play, and how do we prune / summarise
it":

* :func:`get_status` — status-bar snapshot. Cheap O(1) read of
  the latched ``_last_input_tokens`` counter (an earlier async
  implementation went through ``aget_session`` and hung during
  Agno's post-stream tail).
* :func:`count_context_tokens` — locally count tokens of the
  current conversation via Agno's per-provider tokenizer,
  bypassing the wire-side ``input_tokens`` which
  over-inflates on prompt-caching providers.
* :func:`compact_if_needed` — trigger compaction when the
  context ratio crosses the threshold. Returns the summary
  as a ``SessionCleared`` wire message.
* :func:`extract_learnings` — fire-and-forget background task
  that pushes the last user/assistant pair into the learning
  pipeline for memory extraction.
* :func:`truncate_history` — drop a run and every later run
  from the Agno session. Used by edit / delete-message.
* :func:`get_pending_messages` — surface pre-persisted user
  messages that never completed a run (crashed mid-stream).

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from agno.models.message import Message as AgnoMessage
from pydantic import BaseModel

from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


class TruncateHistoryResult(BaseModel):
    """Wire shape for :func:`truncate_history` — ``removed`` is the
    count of runs dropped (0 on any failure); ``error`` is empty on
    success."""

    removed: int
    error: str = ""


class PendingMessage(BaseModel):
    """One pre-persisted user message row surfaced by
    :func:`get_pending_messages`. ``role`` is always ``"user"`` —
    only user turns get pre-persisted, so this is a constant
    on the wire; declared as a plain field (not a discriminator)
    for FE-parity."""

    role: str
    content: str
    received_at: int
    message_id: str


# How stale a pending-message row must be before we surface it
# as an "interrupted" banner. A fresh pending row almost always
# means "Agno is still finishing its post-stream tail" (it can
# take 15-30 s). The banner is meant for actual crashes across
# BE restarts — 60 s makes a reload during the tail stay quiet.
_PENDING_STALENESS_SECONDS = 60


def get_status(backend: "BackendServer") -> msg.StatusUpdate:
    """Get current status bar data.

    Context size comes from the last run's ``input_tokens`` (the
    prompt Agno sent — i.e. the live context). O(1), no DB hit;
    an earlier async implementation called ``aget_session`` and
    hung after a run while Agno's post-stream tail held session
    state.
    """
    # Defensive ``isinstance`` guards: production code always
    # gets a real ``PermissionEvaluator`` here, but test fixtures
    # often pass a ``MagicMock`` session whose
    # ``permission_evaluator.mode.value`` returns a MagicMock —
    # pydantic then rejects the StatusUpdate. The check falls
    # back to ``"default"`` for any non-string value so the wire
    # shape stays valid.
    evaluator = getattr(backend._session, "permission_evaluator", None)
    raw_mode = getattr(getattr(evaluator, "mode", None), "value", None)
    mode = raw_mode if isinstance(raw_mode, str) else "default"
    return msg.StatusUpdate(
        model=backend._settings.models.default,
        cloud_connected=backend._session.cloud_connected,
        cloud_org=backend._session.cloud_org_name or "",
        context_tokens=getattr(backend._session, "_last_input_tokens", 0),
        max_context=backend._settings.models.max_context_window,
        permission_mode=mode,
    )


async def count_context_tokens(backend: "BackendServer") -> int:
    """Locally count the tokens of the current conversation.

    Agno's ``Model.count_tokens`` picks the right tokenizer per
    model (tiktoken for OpenAI-likes, HF for known HF models,
    character estimation otherwise) so we don't roll our own
    per-provider logic. Used by the status-bar context indicator
    and the ``compact_if_needed`` trigger — both used to read
    ``input_tokens`` off the wire, which on prompt-caching
    providers (Anthropic) compounds ``cache_read_input_tokens``
    across tool iterations into millions of tokens and was
    triggering the 80% auto-compaction → history wipe path on
    basically every turn.
    """
    try:
        agno_session = await backend._session.main_team.aget_session(
            session_id=backend._session.session_id,
            user_id=backend._session.user_id,
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
        n = int(backend._session.main_team.model.count_tokens(messages))
    except Exception as exc:
        logger.debug("count_tokens failed (%s); reporting 0", exc)
        return 0
    # Latch only when we actually measured something. Latching
    # 0 turns a transient "session not loaded yet / aget_session
    # raced with attach" into a permanent 0 in the footer until
    # the next run fires — exactly the bug the field saw on
    # session-switch. ``0`` from this RPC means "couldn't
    # measure right now"; leave the previous good value alone
    # and let the next call (or the next run) overwrite.
    if n > 0:
        backend._session._last_input_tokens = n
    return n


async def compact_if_needed(
    backend: "BackendServer",
    ctx_tokens: int,
    max_ctx: int,
) -> msg.SessionCleared | None:
    """Compact session if approaching context limit."""
    compacted = await backend._session.compact_if_needed(ctx_tokens, max_ctx)
    if not compacted:
        return None
    summary = ""
    with contextlib.suppress(Exception):
        agno_session = await backend._session.main_team.aget_session(
            session_id=backend._session.session_id,
            user_id=backend._session.user_id,
        )
        if agno_session and agno_session.summary and agno_session.summary.summary:
            summary = agno_session.summary.summary
    return msg.SessionCleared(
        new_session_id=backend._session.session_id,
        summary=summary,
    )


async def extract_learnings(
    backend: "BackendServer",
    user_msg: str,
    assistant_msg: str,
) -> None:
    """Run learning extraction as a background task on the main event loop.

    Uses the main loop (not a separate thread) so the httpx
    client's connection pool works correctly.
    """
    learning = backend._session._learning
    if learning is None:
        return

    messages = [AgnoMessage(role="user", content=user_msg)]
    if assistant_msg:
        messages.append(AgnoMessage(role="assistant", content=assistant_msg))

    async def _run() -> None:
        try:
            await learning.aprocess(
                messages=messages,
                user_id=backend._session.user_id,
                session_id=backend._session.session_id,
            )
        except Exception as exc:
            logger.warning("Learning extraction failed: %s", exc)

    asyncio.create_task(_run())


async def truncate_history(
    backend: "BackendServer",
    session_id: str,
    run_id: str,
) -> TruncateHistoryResult:
    """Drop the run with ``run_id`` and every later run from the
    session. Used by the FE when the user edits or deletes one
    of their past messages — both operations require that
    everything downstream of the touched turn gets wiped before
    continuing. ``removed=0`` on any failure path.
    """
    agent = backend._session.main_team
    agno_session = await agent.aget_session(
        session_id=session_id,
        user_id=backend._session.user_id,
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
        return TruncateHistoryResult(
            removed=0, error=f"run_id {run_id!r} not in session"
        )
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
    backend._session._last_input_tokens = 0
    return TruncateHistoryResult(removed=removed)


async def get_pending_messages(
    backend: "BackendServer",
    session_id: str,
) -> list[PendingMessage]:
    """Pending user messages that never completed a run.

    Surfaced by the FE on ``--continue`` to render the user's
    interrupted question(s) in the conversation pane — Agno's
    own ``get_chat_history`` doesn't return them because Agno
    only persists at end-of-run, so a crash mid-stream leaves
    the message visible only in our pre-persistence table.
    Oldest-first order.
    """
    try:
        rows = await backend._pending_store.alist_pending(session_id)
    except Exception as exc:
        logger.debug("get_pending_messages failed: %s", exc)
        return []
    # Filter to rows older than the staleness threshold — see
    # module-level ``_PENDING_STALENESS_SECONDS`` for why.
    cutoff = int(time.time()) - _PENDING_STALENESS_SECONDS
    rows = [r for r in rows if r.received_at <= cutoff]
    return [
        PendingMessage(
            role="user",
            content=r.text,
            received_at=r.received_at,
            message_id=r.message_id,
        )
        for r in rows
    ]
