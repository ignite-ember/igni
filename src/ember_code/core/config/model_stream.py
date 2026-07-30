"""Tool-call argument streaming primitives.

Extracted from :mod:`ember_code.core.config.models` so the model
registry / logging-model concerns stay small. Everything here is
about turning an OpenAI-compatible model stream's ``tool_calls``
deltas into a series of ``CustomEvent(event='tool_call_input_delta')``
that the FE picks up for progressive tool-call rendering.

The three levels:

* :class:`_ToolCallFragment` — one delta pulled off a streaming chunk.
* :class:`_ToolCallAccumulator` — running state for one in-flight tool
  call, populated across successive fragments.
* :class:`_ToolCallAccumulatorStore` — per-stream cache keyed by
  tool-call ``index``. One instance per model stream.

:func:`_emit_tool_arg_delta_events` turns one chunk + the store into
zero or more ``CustomEvent`` objects. The sync + async generator
wrappers (:func:`_emit_tool_arg_deltas`, :func:`_aemit_tool_arg_deltas`)
own the store's lifetime — creating a fresh one per stream so no
cross-stream leakage is possible.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.models.response import ModelResponse
from agno.run.agent import CustomEvent
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _ToolCallFragment(BaseModel):
    """One tool-call delta pulled off a streaming chunk.

    OpenAI-compatible streams deliver ``choice_delta.tool_calls`` as a
    list where each entry has ``.index``, ``.id`` (populated on the
    FIRST delta only), and ``.function`` with ``.name`` (first delta
    only) + ``.arguments`` (incremental string fragment on every
    delta). Only ``index`` is present on EVERY delta, so the caller
    uses that as the stable accumulator key and remembers the ``id`` /
    ``name`` when they land on the first delta.
    """

    index: int | None = None
    call_id: str | None = None
    name: str | None = None
    args_fragment: str | None = None

    @classmethod
    def from_provider(cls, tc: Any) -> _ToolCallFragment:
        """Accept either an SDK model object or a dict — Agno
        providers vary in what they hand back."""
        if hasattr(tc, "function"):
            fn = tc.function
            return cls(
                index=getattr(tc, "index", None),
                call_id=getattr(tc, "id", None),
                name=getattr(fn, "name", None) if fn else None,
                args_fragment=getattr(fn, "arguments", None) if fn else None,
            )
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            return cls(
                index=tc.get("index"),
                call_id=tc.get("id"),
                name=fn.get("name"),
                args_fragment=fn.get("arguments"),
            )
        return cls()


class _ToolCallAccumulator(BaseModel):
    """Running state for one in-flight tool call. Populated across
    successive streaming deltas."""

    call_id: str = ""
    name: str = ""
    args: str = ""


class _ToolCallAccumulatorStore(BaseModel):
    """Per-stream cache keyed by tool-call INDEX. Each entry holds
    the running ``call_id``, ``name``, and accumulated ``args``. We
    mutate this in place across deltas so subsequent chunks see the
    accumulated state — one instance per model stream, naturally
    scoped so no cross-stream leakage is possible."""

    by_index: dict[int, _ToolCallAccumulator] = Field(default_factory=dict)

    def apply(self, fragment: _ToolCallFragment) -> _ToolCallAccumulator | None:
        """Merge one fragment into the store and return the updated
        accumulator IFF this fragment carried new argument bytes
        (i.e. worth emitting a delta event for). A first fragment
        that only carries id + name returns ``None`` — no wire
        traffic warranted."""
        if fragment.index is None:
            return None
        entry = self.by_index.setdefault(fragment.index, _ToolCallAccumulator())
        if fragment.call_id and not entry.call_id:
            entry.call_id = fragment.call_id
        if fragment.name and not entry.name:
            entry.name = fragment.name
        if not fragment.args_fragment:
            return None
        entry.args += fragment.args_fragment
        return entry


def _emit_tool_arg_delta_events(
    chunk: Any,
    store: _ToolCallAccumulatorStore,
) -> list[CustomEvent]:
    """Given one streaming chunk from the model, return the list of
    ``CustomEvent(event='tool_call_input_delta', ...)`` to yield after
    it. Empty on the hot path (text_delta only).

    Defensive: any exception raised here — Pydantic validation on an
    unexpected chunk shape, string-arithmetic on non-string args,
    etc. — is caught and swallowed. The reason is critical:
    Agno's ``aprocess_response_stream`` runs
    ``_populate_assistant_message_from_stream_data`` AFTER its
    ``async for`` loop finishes. If an exception in this function
    propagates up through the wrapper, that post-loop call NEVER
    runs, which leaves ``assistant_message.tool_calls`` empty and
    downstream tool execution sees truncated / malformed JSON ("'{'
    was never closed"). Losing progressive rendering on this chunk
    is far cheaper than corrupting the tool call.
    """
    if not isinstance(chunk, ModelResponse):
        return []
    if not chunk.tool_calls:
        return []
    events: list[CustomEvent] = []
    try:
        for tc in chunk.tool_calls:
            fragment = _ToolCallFragment.from_provider(tc)
            entry = store.apply(fragment)
            if entry is None:
                continue
            events.append(
                CustomEvent(
                    event="tool_call_input_delta",
                    tool_call_id=entry.call_id,
                    tool_name=entry.name,
                    arguments_partial=entry.args,
                )
            )
    except Exception as exc:
        # See docstring — never let this interrupt Agno's stream.
        logger.debug("tool_arg_delta emission failed on chunk: %s", exc)
        return []
    return events


def _emit_tool_arg_deltas(source):
    """Sync generator: wrap the parent ``process_response_stream``
    output, forwarding chunks unchanged and interleaving
    ``CustomEvent`` deltas for tool-call arguments as they land.

    The accumulator store lives in this generator's local frame, so
    each model stream gets its own — no shared-state risk across
    concurrent model calls.

    The chunk is yielded BEFORE our CustomEvent-emission runs, and
    the emission itself is exception-safe (see
    :func:`_emit_tool_arg_delta_events`). Together this guarantees
    a well-formed chunk always reaches Agno's accumulator even if
    our progressive-rendering hook can't be built for it.
    """
    store = _ToolCallAccumulatorStore()
    for chunk in source:
        yield chunk
        yield from _emit_tool_arg_delta_events(chunk, store)


async def _aemit_tool_arg_deltas(source):
    """Async counterpart to :func:`_emit_tool_arg_deltas`."""
    store = _ToolCallAccumulatorStore()
    async for chunk in source:
        yield chunk
        for ev in _emit_tool_arg_delta_events(chunk, store):
            yield ev
