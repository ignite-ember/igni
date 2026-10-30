"""How big a conversation is, without loading a tokenizer.

The model server already counts: every answer reports the input tokens
it read and the output tokens it wrote, and agno keeps those on the
assistant message (``Message.metrics``). So the size of a conversation
up to its last answer is known exactly — that answer's input plus its
output. Only what came after it (tool results, the next user message)
has never been counted, and that tail is estimated from its length.

This replaced agno's ``count_tokens``, which loads tiktoken's
``o200k_base`` encoding: about 80 MB that stayed resident from the
first message on, to count text the server had already counted. The
estimate is ``len / CHARS_PER_TOKEN``; it only ever covers the tail, so
its error is a few percent of the tail, not of the conversation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


class TokenEstimate:
    """Token counts read off the server's own numbers, with a length estimate for the rest."""

    #: Characters per token for text nobody has counted yet. Measured
    #: against o200k (``tests/fixtures/o200k_token_counts.json``): prose
    #: and Markdown ~3.7–3.9, JSON ~3.4, Python and TypeScript ~4.3–4.4.
    #: 4 keeps every kind within ±15%.
    CHARS_PER_TOKEN = 4.0

    @classmethod
    def of(cls, messages: Sequence[Any], tools: Sequence[Any] | None = None) -> int:
        """The conversation's size: the last answer's measured context, plus the tail after it."""
        measured_at = cls._last_measured(messages)
        if measured_at is None:
            return cls.chars(messages) + cls.tool_chars(tools)
        answer = messages[measured_at]
        measured = cls._input(answer) + cls._output(answer)
        return measured + cls.chars(messages[measured_at + 1 :])

    @classmethod
    def chars(cls, messages: Sequence[Any]) -> int:
        """The estimate for messages nobody has counted."""
        return cls.text(sum(cls._length(m) for m in messages))

    @classmethod
    def tool_chars(cls, tools: Sequence[Any] | None) -> int:
        """The estimate for tool schemas, as the model receives them."""
        if not tools:
            return 0
        return cls.text(sum(len(cls._schema(tool)) for tool in tools))

    @classmethod
    def text(cls, length: int) -> int:
        return int(length / cls.CHARS_PER_TOKEN + 0.5) if length > 0 else 0

    # ── reading agno's messages ───────────────────────────────────

    @classmethod
    def _last_measured(cls, messages: Sequence[Any]) -> int | None:
        for i in range(len(messages) - 1, -1, -1):
            if getattr(messages[i], "role", None) == "assistant" and cls._input(messages[i]) > 0:
                return i
        return None

    @staticmethod
    def _input(message: Any) -> int:
        metrics = getattr(message, "metrics", None)
        return int(getattr(metrics, "input_tokens", 0) or 0)

    @staticmethod
    def _output(message: Any) -> int:
        metrics = getattr(message, "metrics", None)
        return int(getattr(metrics, "output_tokens", 0) or 0)

    @staticmethod
    def _length(message: Any) -> int:
        content = getattr(message, "compressed_content", None) or getattr(message, "content", None)
        if content is None:
            length = 0
        elif isinstance(content, str):
            length = len(content)
        else:
            length = len(json.dumps(content, default=str))
        for call in getattr(message, "tool_calls", None) or []:
            length += len(json.dumps(call, default=str))
        return length

    @staticmethod
    def _schema(tool: Any) -> str:
        if isinstance(tool, dict):
            return json.dumps(tool, default=str)
        to_dict = getattr(tool, "to_dict", None)
        if callable(to_dict):
            return json.dumps(to_dict(), default=str)
        return str(getattr(tool, "name", "")) + str(getattr(tool, "description", ""))


__all__ = ["TokenEstimate"]
