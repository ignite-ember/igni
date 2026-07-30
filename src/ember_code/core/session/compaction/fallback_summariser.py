"""MiniMax-compatible fallback summariser for session compaction.

Why this exists (the class name is the documentation — this
docstring collects what used to be four scattered comments in
the old ``compact_ops`` module):

Agno's :class:`SessionSummaryManager` hands the model a JSON-
structured prompt. MiniMax-M2.7 (and other reasoning models we
target) *often* returns from ``acreate_session_summary`` without
raising, but leaves the summary object empty — the schema
prompt is too demanding to fill in reliably. When that happens
the user sees a silently-empty summary card and the compaction
looks broken.

:class:`FallbackSummariser` runs a plain free-text prompt over
a compacted transcript. It:

* Extracts a bounded transcript from the session's runs (user +
  assistant text only — tool calls and internal events are
  noise for a *summarise-the-context* call and would risk
  hitting the context cap of the summariser call itself).
* Caps per-message length so one long response doesn't dominate
  the prompt.
* Strips ``<think>…</think>`` blocks defensively — MiniMax leaks
  them through even when instructed not to, including unclosed
  ones at end-of-string.

Agno message rows arrive in two shapes (attribute-access
``Message`` objects and dict-shaped rows persisted from earlier
sessions); :class:`TranscriptMessage.from_agno_message` isolates
the dual-access branch in one place.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from agno.models.message import Message as AgnoMessage
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from pydantic import BaseModel

AgnoSession = AgentSession | TeamSession

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class TranscriptMessage(BaseModel):
    """Normalised transcript row extracted from an Agno run.

    Agno's persistence layer stores messages as either attribute-
    access ``Message`` objects (fresh runs) or plain dicts
    (rehydrated from older DB rows). Callers previously had to
    handle both shapes at every access site
    (``getattr(m, "role", None) or m.get("role")``). This model
    normalises via :meth:`from_agno_message` so downstream code
    only sees typed fields.
    """

    role: str
    content: str

    @classmethod
    def from_agno_message(cls, message: Any) -> TranscriptMessage | None:
        """Build a :class:`TranscriptMessage` from either an Agno
        ``Message`` object or a dict-shaped row.

        Returns ``None`` when the row is not a user / assistant
        text message (tool calls, internal events, empty content)
        so callers can filter with a simple ``is None`` check.
        """
        role = getattr(message, "role", None)
        if role is None and hasattr(message, "get"):
            role = message.get("role")
        content = getattr(message, "content", None)
        if content is None and hasattr(message, "get"):
            content = message.get("content")
        if role not in ("user", "assistant"):
            return None
        if not isinstance(content, str) or not content.strip():
            return None
        return cls(role=role, content=content.strip())


class FallbackSummariser:
    """Plain free-text summariser used when Agno's structured
    summariser returns nothing.

    Composed by :class:`CompactionCoordinator`; not intended
    for direct use elsewhere. Holds a reference to the session
    so it can reach the live ``main_team.model`` at call time
    (the model is reassigned when the user switches provider
    mid-session).
    """

    _TRANSCRIPT_MSG_CAP = 800
    _TRANSCRIPT_TAIL = 60

    def __init__(self, session: Session) -> None:
        self._session = session

    async def summarise(self, agno_session: AgnoSession) -> str:
        """Return a free-text summary of ``agno_session``'s runs.

        Empty string when there's nothing to summarise (no runs,
        no user/assistant text), when the model call fails, or
        when the model returns a non-string response.
        """
        transcript = self._build_transcript(agno_session)
        if not transcript:
            return ""

        prompt = self._build_prompt(transcript)
        model = self._session.main_team.model
        try:
            resp = await model.aresponse(messages=[AgnoMessage(role="user", content=prompt)])
        except Exception as e:
            logger.warning("fallback aresponse failed: %s", e)
            return ""

        text = getattr(resp, "content", None) or ""
        if not isinstance(text, str):
            return ""
        return self._strip_think_tags(text)

    def _build_transcript(self, agno_session: AgnoSession) -> str:
        """Pull a compact transcript from the session's runs.

        Only ``user`` / ``assistant`` text messages contribute;
        tool calls and internal events would bloat the prompt
        and risk hitting the context cap for this
        *summarise-the-context* call. Per-message content is
        capped at :attr:`_TRANSCRIPT_MSG_CAP` characters so one
        long response can't dominate the prompt. The last
        :attr:`_TRANSCRIPT_TAIL` normalised rows are joined into
        the final transcript.
        """
        lines: list[str] = []
        for run in getattr(agno_session, "runs", None) or []:
            try:
                for raw in getattr(run, "messages", None) or []:
                    msg = TranscriptMessage.from_agno_message(raw)
                    if msg is None:
                        continue
                    snippet = msg.content
                    if len(snippet) > self._TRANSCRIPT_MSG_CAP:
                        snippet = snippet[: self._TRANSCRIPT_MSG_CAP] + "…"
                    lines.append(f"{msg.role.upper()}: {snippet}")
            except Exception:
                continue
        if not lines:
            return ""
        return "\n\n".join(lines[-self._TRANSCRIPT_TAIL :])

    @staticmethod
    def _build_prompt(transcript: str) -> str:
        return (
            "Summarise the following conversation in 4-8 sentences. "
            "Focus on what the user asked, what the assistant did, and any "
            "open threads. Output ONLY the summary text — no preamble, no "
            "JSON, no <think> tags, no markdown headers.\n\n"
            "--- CONVERSATION ---\n"
            f"{transcript}\n"
            "--- END ---"
        )

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """Strip ``<think>…</think>`` blocks — including unclosed
        ones at end-of-string. MiniMax leaks them through even
        when the prompt tells it not to."""
        return re.sub(r"<think>[\s\S]*?(</think>\s*|$)", "", text).strip()
