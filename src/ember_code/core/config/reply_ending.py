"""How a model's reply ended — and a cut one is an error, not a success.

An OpenAI-compatible reply ends with a ``finish_reason``: ``stop`` or
``tool_calls`` when the model finished, ``length`` when it ran into the
most it may write in one reply. Agno reads none of it. A stream that
stopped with no ``finish_reason`` at all (the connection to the model
dropped — igni__server's SSE keepalive closed upstream streams this way
on the first silence longer than its interval) and one cut at ``length``
(igni-1.0-4b reasoning through its whole 4,096-token budget) both reached
igni as a finished reply: ``model_completed`` and ``run_completed`` with
0 tokens and an empty answer, and no ``run_error``.

:class:`ReplyEnding` watches one reply for its ``finish_reason`` and
raises :class:`ModelReplyCut` when the reply did not finish, so the run
ends in a ``run_error`` every client shows. It is a
:class:`~agno.exceptions.ModelProviderError` for Agno's error handling;
its message avoids the phrases Agno reads as "context window exceeded"
(``ModelProviderError.CONTEXT_WINDOW_PATTERNS``), which would send it
down the compaction path instead.

The same rule holds for every provider igni builds, each saying it its
own way: Anthropic's ``stop_reason`` (``max_tokens`` is the length cut,
``refusal`` a stop before the end) and Gemini's ``finish_reason``
(``MAX_TOKENS``; ``SAFETY``, ``RECITATION`` and the other filters stop a
reply early). :class:`Provider` holds each one's words for "finished"
and "ran out of room"; anything else a provider says is a reply stopped
before it finished, and no word at all is a dropped stream.

No tolerant mode for an OpenAI-compatible server that never sends a
``finish_reason``: every server igni is pointed at sends one on the last
chunk — vLLM, llama.cpp's ``llama-server``, mlx-lm's ``mlx_lm.server``
(``stop``/``length``/``tool_calls``, in its final streamed response) and
Ollama's ``/v1`` endpoint — and a reply without one is exactly what a
proxy closing the stream early looks like. Excusing it would bring that
silent success back for everyone to spare a server nobody has named.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any, ClassVar

from agno.exceptions import ModelProviderError
from pydantic import BaseModel, ConfigDict

#: The ``finish_reason`` of a reply that ran into its output cap.
LENGTH = "length"


class Provider(BaseModel):
    """How one provider says a reply ended.

    ``finished`` is ``None`` for OpenAI-compatible servers: any reason but
    the length cut counts as finished there (``stop``, ``tool_calls``,
    ``function_call`` and whatever a server adds), as before.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    length: frozenset[str]
    finished: frozenset[str] | None = None

    def classify(self, reason: str) -> str:
        """``"finished"``, ``"length"`` or ``"stopped"``."""
        if reason in self.length:
            return "length"
        if self.finished is None or reason in self.finished:
            return "finished"
        return "stopped"


OPENAI = Provider(name="openai", length=frozenset({LENGTH}))
#: Anthropic's ``stop_reason``. ``pause_turn`` is a long server-tool turn
#: handed back to be continued, not a cut; ``model_context_window_exceeded``
#: is the reply running out of room, as ``max_tokens`` is.
ANTHROPIC = Provider(
    name="anthropic",
    length=frozenset({"max_tokens", "model_context_window_exceeded"}),
    finished=frozenset({"end_turn", "tool_use", "stop_sequence", "pause_turn"}),
)
#: Gemini's ``finish_reason`` (``google.genai.types.FinishReason``).
GEMINI = Provider(name="gemini", length=frozenset({"MAX_TOKENS"}), finished=frozenset({"STOP"}))


class ModelReplyCut(ModelProviderError):
    """The model's reply did not finish."""

    @classmethod
    def no_ending(cls, model_id: str) -> ModelReplyCut:
        return cls(
            "The model's reply was cut off: its stream ended without a "
            "finish_reason, so the connection to the model was probably "
            "dropped. Nothing after that point arrived; try again.",
            model_name=model_id,
            model_id=model_id,
        )

    @classmethod
    def stopped(cls, model_id: str, reason: str) -> ModelReplyCut:
        # The provider's word is quoted unless it carries a phrase Agno
        # reads as a context-window error.
        said = (
            f" ({reason})"
            if not any(p in reason.lower() for p in ModelProviderError.CONTEXT_WINDOW_PATTERNS)
            else ""
        )
        return cls(
            f"The model's reply was stopped before it finished{said}: the "
            "provider ended it early, for example by a content filter. "
            "Nothing after that point arrived; rephrase or try again.",
            model_name=model_id,
            model_id=model_id,
        )

    @classmethod
    def at_length(cls, model_id: str, cap: int | None) -> ModelReplyCut:
        limit = f" ({cap} tokens)" if cap else ""
        return cls(
            f"The model's reply was cut off at its output length limit{limit} "
            "before it finished (finish_reason=length). Try again, or ask "
            "for a shorter answer.",
            model_name=model_id,
            model_id=model_id,
        )


class ReplyEnding:
    """The ``finish_reason`` of one model reply, as it streams in.

    A stream's chunks are parsed by the model's
    ``_parse_provider_response_delta``, which does not know which call it
    is parsing for; the reply being watched is found through a context
    variable, so two replies streaming at once on one shared model (a
    spawned agent is a shallow copy of the pool's) are not confused.
    """

    _current: ClassVar[ContextVar[ReplyEnding | None]] = ContextVar(
        "ember_reply_ending", default=None
    )

    def __init__(self, model_id: str, cap: int | None, provider: Provider = OPENAI) -> None:
        self._model_id = model_id
        self._cap = cap
        self._provider = provider
        self.reason: str | None = None

    @classmethod
    def note_chunk(cls, chunk: Any) -> None:
        """Record ``chunk``'s ending on the reply being watched, when
        there is one and the chunk carries one."""
        watching = cls._current.get()
        if watching is None:
            return
        reason = reason_in(chunk)
        if reason:
            watching.reason = reason

    @contextlib.contextmanager
    def watching(self) -> Iterator[ReplyEnding]:
        token = self._current.set(self)
        try:
            yield self
        finally:
            # A generator closed from another context cannot reset its
            # token; the variable dies with that context anyway.
            with contextlib.suppress(ValueError):
                self._current.reset(token)

    def check(self) -> None:
        """Raise :class:`ModelReplyCut` unless the reply finished."""
        if self.reason is None:
            raise ModelReplyCut.no_ending(self._model_id)
        verdict = self._provider.classify(self.reason)
        if verdict == "length":
            raise ModelReplyCut.at_length(self._model_id, self._cap)
        if verdict == "stopped":
            raise ModelReplyCut.stopped(self._model_id, self.reason)

    @classmethod
    def check_completion(
        cls, completion: Any, model_id: str, cap: int | None, provider: Provider = OPENAI
    ) -> None:
        """The same check on a whole (non-streamed) completion."""
        ending = cls(model_id, cap, provider)
        ending.reason = reason_in(completion)
        ending.check()


def _word(value: Any) -> str | None:
    """A provider's reason as a plain string (Gemini's is an enum)."""
    if value is None:
        return None
    value = getattr(value, "value", value)
    return str(value) or None


def reason_in(response: Any) -> str | None:
    """The ending one response or stream chunk carries, from any provider.

    OpenAI-compatible: ``choices[].finish_reason``. Gemini:
    ``candidates[].finish_reason``. Anthropic: ``stop_reason`` on a whole
    message, on the ``message_delta`` event's ``delta`` and on the
    ``message_stop`` event's accumulated ``message``.
    """
    found: str | None = None
    for choice in getattr(response, "choices", None) or []:
        found = _word(getattr(choice, "finish_reason", None)) or found
    for candidate in getattr(response, "candidates", None) or []:
        found = _word(getattr(candidate, "finish_reason", None)) or found
    for holder in (
        response,
        getattr(response, "delta", None),
        getattr(response, "message", None),
    ):
        stop = getattr(holder, "stop_reason", None) if holder is not None else None
        if isinstance(stop, str) and stop:
            found = stop
    return found
