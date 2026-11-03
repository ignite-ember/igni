"""Ask the model for a session title — a small, bounded, async call.

What this replaced: Agno's ``aset_session_name``. Despite its name it
calls the model *synchronously* (``agent.model.response`` in
``agno/agent/_session.py``) on the event loop, sends the whole
conversation — the ~12k-token system prompt included — and asks again
up to three more times whenever the answer runs past five words. A
small model answers the conversation instead of naming it, so every
new session cost four uncached ~12k-token calls on the only GPU slot,
and the loop was blocked for each of them long enough for the
backend to miss its WebSocket pings and drop the client mid-task
(``1011 keepalive ping timeout``).

:class:`TitleAsker` sends only what a title is made from — the
person's first message(s), capped in length — with a token budget,
through the model's async ``aresponse``. One retry at most; when no
usable title comes back, or when the model is busy with the person's
work, the title is the opening of the first message, which is what a
person would write anyway.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, ClassVar

from agno.models.message import Message as AgnoMessage

from ember_code.core.session.schemas import SessionTitle

logger = logging.getLogger(__name__)


class TitleAsker:
    """Turns a session's opening message(s) into a short title."""

    # What the model sees: the opening, never the conversation.
    MAX_OPENING_CHARS: ClassVar[int] = 600
    # Room for a reasoning model's short think before the answer
    # (the 4B spends ~50-130 tokens on it), and no more.
    MAX_TITLE_TOKENS: ClassVar[int] = 256
    # One ask and one retry.
    MAX_ATTEMPTS: ClassVar[int] = 2
    MAX_TITLE_WORDS: ClassVar[int] = 6
    MAX_TITLE_CHARS: ClassVar[int] = 60

    _FALLBACK_WORDS: ClassVar[int] = 6
    _FALLBACK_CHARS: ClassVar[int] = 50

    _SYSTEM_CONTEXT_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"<system-context>[\s\S]*?</system-context>\s*"
    )
    _EXTENSION_TAG_RE: ClassVar[re.Pattern[str]] = re.compile(r"</?extension-message[^>]*>")
    _THINK_RE: ClassVar[re.Pattern[str]] = re.compile(r"<think>[\s\S]*?(</think>\s*|$)")

    _INSTRUCTION: ClassVar[str] = (
        "You write titles for chat sessions. Reply with a title of at most "
        "five words for the request below. Reply with the title only: do not "
        "answer the request, do not explain, no quotes, no punctuation at the end."
    )
    _RETRY_INSTRUCTION: ClassVar[str] = (
        "Your last reply was not a title. Reply with two to five words that "
        "name the topic of the request below, and nothing else."
    )

    @classmethod
    def opening(cls, user_texts: list[str]) -> str:
        """The person's words from the first message(s), capped.

        Drops the ``<system-context>`` blocks igni adds (the datetime
        hint, an extension's attribution) and an extension message's
        tag, so the title is made from what was asked.
        """
        parts: list[str] = []
        for text in user_texts:
            cleaned = cls._SYSTEM_CONTEXT_RE.sub("", text)
            cleaned = cls._EXTENSION_TAG_RE.sub("", cleaned).strip()
            if cleaned:
                parts.append(cleaned)
        return "\n\n".join(parts)[: cls.MAX_OPENING_CHARS].strip()

    @classmethod
    def fallback(cls, opening: str) -> str:
        """A title that needs no model: the opening's first words."""
        words = " ".join(opening.split()).split(" ")
        title = " ".join(words[: cls._FALLBACK_WORDS])
        cut = len(words) > cls._FALLBACK_WORDS
        if len(title) > cls._FALLBACK_CHARS:
            title = title[: cls._FALLBACK_CHARS].rsplit(" ", 1)[0] or title[: cls._FALLBACK_CHARS]
            cut = True
        title = title.rstrip(" .,:;!?")
        return f"{title}…" if cut and title else title

    @classmethod
    def usable(cls, answer: str) -> str:
        """The answer as a title, or ``""`` when it is not one."""
        text = cls._THINK_RE.sub("", answer or "").strip()
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) != 1:
            return ""
        title = SessionTitle.clean(lines[0]).rstrip(" .")
        if not title:
            return ""
        if len(title.split()) > cls.MAX_TITLE_WORDS or len(title) > cls.MAX_TITLE_CHARS:
            return ""
        return title

    def __init__(self, model: Any) -> None:
        self._model = self._bounded(model)

    async def ask(self, opening: str) -> str:
        """A title for ``opening``: the model's, or the fallback."""
        if not opening:
            return ""
        if self._model is not None:
            for attempt in range(self.MAX_ATTEMPTS):
                instruction = self._INSTRUCTION if attempt == 0 else self._RETRY_INSTRUCTION
                title = self.usable(await self._call(instruction, opening))
                if title:
                    return title
        return self.fallback(opening)

    async def _call(self, instruction: str, opening: str) -> str:
        messages = [
            AgnoMessage(role="system", content=instruction),
            AgnoMessage(role="user", content=f"Request:\n{opening}\n\nTitle:"),
        ]
        try:
            response = await self._model.aresponse(messages=messages)
        except Exception as exc:
            logger.debug("session title call failed: %s", exc)
            return ""
        content = getattr(response, "content", None)
        return content if isinstance(content, str) else ""

    @classmethod
    def _bounded(cls, model: Any) -> Any:
        """A copy of ``model`` that answers in at most
        :attr:`MAX_TITLE_TOKENS` — the live model is left as it is."""
        if model is None:
            return None
        bounded = copy.copy(model)
        try:
            bounded.max_tokens = cls.MAX_TITLE_TOKENS
        except AttributeError:
            logger.debug("model %r takes no max_tokens; title call unbounded", model)
        return bounded
