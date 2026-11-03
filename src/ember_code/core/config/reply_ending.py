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
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any, ClassVar

from agno.exceptions import ModelProviderError

#: The ``finish_reason`` of a reply that ran into its output cap.
LENGTH = "length"


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

    def __init__(self, model_id: str, cap: int | None) -> None:
        self._model_id = model_id
        self._cap = cap
        self.reason: str | None = None

    @classmethod
    def note_chunk(cls, chunk: Any) -> None:
        """Record ``chunk``'s ``finish_reason`` on the reply being
        watched, when there is one and the chunk carries one."""
        watching = cls._current.get()
        if watching is None:
            return
        for choice in getattr(chunk, "choices", None) or []:
            reason = getattr(choice, "finish_reason", None)
            if reason:
                watching.reason = str(reason)

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
        if self.reason == LENGTH:
            raise ModelReplyCut.at_length(self._model_id, self._cap)

    @classmethod
    def check_completion(cls, completion: Any, model_id: str, cap: int | None) -> None:
        """The same check on a whole (non-streamed) completion."""
        ending = cls(model_id, cap)
        for choice in getattr(completion, "choices", None) or []:
            reason = getattr(choice, "finish_reason", None)
            if reason:
                ending.reason = str(reason)
        ending.check()
