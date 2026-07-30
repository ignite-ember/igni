"""Agno RunOutput adapter — one home for Agno-shape knowledge.

Every quirk in how Agno reports a run — ``from_history=True`` message
replay, errored tool-call scrubbing, tool_call_id-tracked rejections,
``RunOutput.tools`` walking for the trace — lives here. The runner no
longer needs to know Agno's shape.

Why the getattr() defensiveness stays:
    Agno's ``RunOutput.messages`` items expose fields as attributes on
    Pydantic-like models, but tests build these with ``MagicMock`` and
    real Agno message subclasses have historically dropped/renamed
    fields between minor versions. ``getattr(m, "…", default)`` keeps
    the eval framework working across a small version window without
    turning every attribute access into a hard fail.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from ember_code.core.evals.schemas import ToolTraceEntry

logger = logging.getLogger(__name__)


class AgnoResponseAdapter:
    """Wraps an Agno ``RunOutput`` and normalises it for eval checks.

    Construct with the raw response object, then chain the mutators
    (:meth:`strip_from_history`, :meth:`strip_errored_tool_calls`) and
    finally read the derived views (:meth:`tool_trace`,
    :attr:`output_text`, :attr:`response`).

    The mutators return ``self`` for chaining. All mutations happen
    on a shallow copy of the response — the original ``RunOutput``
    the agent produced is never mutated in place.
    """

    #: Specific markers that uniquely identify a runtime rejection of an
    #: unknown/invalid tool call. Generic strings like "not found" are
    #: dangerous — tool results often contain "no matches found", "file
    #: not found" etc. as legitimate output. Stripping those would erase
    #: real tool calls and the reliability check would report every call
    #: as missing.
    ERROR_MARKERS: tuple[str, ...] = (
        "Function ",  # Agno: "Function X not found" — only emitted when the runtime rejects a tool name
        "ValidationError",  # pydantic: invalid args to a real tool
        "Unexpected keyword argument",  # pydantic kwargs mismatch
        "Missing required argument",  # pydantic positional/required mismatch
    )

    def __init__(self, response: Any) -> None:
        self._response = response

    # ── Properties ─────────────────────────────────────────────────

    @property
    def response(self) -> Any:
        """The (possibly-mutated) Agno RunOutput object."""
        return self._response

    @property
    def output_text(self) -> str:
        """Coerce ``response.content`` to a string.

        Agno may return dict / model / plain str content depending on
        the agent's output_type. Downstream AccuracyEval expects a str.
        """
        content = getattr(self._response, "content", None)
        if isinstance(content, str):
            return content
        if content is not None:
            return str(content)
        return ""

    def has_messages(self) -> bool:
        """True when the response has a non-None ``messages`` list.

        Agno's ReliabilityEval crashes with ``'NoneType' is not
        reversible`` when messages is None (typically because the
        underlying LLM run errored out). Guard that case upstream so an
        API failure surfaces as a clean error string, not a stack trace
        pretending to be a tool-call mismatch.
        """
        return self._response is not None and getattr(self._response, "messages", None) is not None

    # ── Mutators (chainable) ────────────────────────────────────────

    def strip_from_history(self) -> AgnoResponseAdapter:
        """Drop ``from_history=True`` messages from the response.

        Prior_messages sent on the same session_id get reloaded into
        ``response.messages`` with ``from_history=True``. Without this
        filter, multi-turn cases false-fail on tools the agent called
        in turn 1: tool_trace extraction, unexpected_tool_calls, and
        ReliabilityEval each see stale tool calls from previous turns.
        """
        msgs = list(getattr(self._response, "messages", None) or [])
        current_only = [m for m in msgs if not getattr(m, "from_history", False)]
        if len(current_only) != len(msgs):
            self._response = copy.copy(self._response)
            self._response.messages = current_only
        return self

    def strip_errored_tool_calls(self) -> AgnoResponseAdapter:
        """Remove tool calls whose runtime replies signalled a rejection.

        A tool call is considered errored when its corresponding
        ``role='tool'`` reply has ``tool_call_error`` set OR the content
        starts with one of :attr:`ERROR_MARKERS`. When the model
        hallucinates a tool name like ``"Read"`` and Agno rejects it
        ("Function Read not found"), the rejected call still lands in
        the message log. Counting these as real tool calls produces
        false reliability failures — the agent didn't actually use them,
        the runtime refused.
        """
        messages = list(getattr(self._response, "messages", None) or [])
        errored_ids = self._collect_errored_tool_call_ids(messages)
        if not errored_ids:
            return self

        cleaned = self._filter_errored_calls(messages, errored_ids)
        self._response = copy.copy(self._response)
        self._response.messages = cleaned
        return self

    def _collect_errored_tool_call_ids(self, messages: list[Any]) -> set[str]:
        errored_ids: set[str] = set()
        for m in messages:
            if getattr(m, "role", None) != "tool":
                continue
            if getattr(m, "tool_call_error", False):
                tcid = getattr(m, "tool_call_id", None)
                if tcid:
                    errored_ids.add(tcid)
                continue
            content = getattr(m, "content", None)
            if isinstance(content, str) and any(mk in content for mk in self.ERROR_MARKERS):
                tcid = getattr(m, "tool_call_id", None)
                if tcid:
                    errored_ids.add(tcid)
        return errored_ids

    @staticmethod
    def _filter_errored_calls(messages: list[Any], errored_ids: set[str]) -> list[Any]:
        cleaned: list[Any] = []
        for m in messages:
            tcs = getattr(m, "tool_calls", None)
            if tcs:
                kept = [
                    tc for tc in tcs if (tc.get("id") or tc.get("tool_call_id")) not in errored_ids
                ]
                if len(kept) != len(tcs):
                    try:
                        m2 = copy.copy(m)
                        m2.tool_calls = kept if kept else None
                        cleaned.append(m2)
                        continue
                    except Exception:
                        pass
            cleaned.append(m)
        return cleaned

    # ── Derived views ───────────────────────────────────────────────

    def tool_trace(self) -> list[ToolTraceEntry]:
        """Walk ``response.tools`` and build a serialisable trace.

        Truncates result previews to ~400 chars so the JSON dump stays
        readable. Coerces ``tool_args`` to a plain dict — MagicMocks in
        tests (and any hypothetical Agno version drift) can leak non-dict
        objects; rejecting them here lets the schema validator catch
        shape drift up front instead of downstream ``args.get(...)``
        crashes in the tool-arg assertion check.
        """
        trace: list[ToolTraceEntry] = []
        tools = getattr(self._response, "tools", None) or []
        for t in tools:
            tname = getattr(t, "tool_name", None)
            if not tname:
                continue
            raw_result = getattr(t, "result", None)
            preview = ""
            if raw_result is not None:
                s = str(raw_result)
                preview = s if len(s) <= 400 else s[:397] + "..."
            raw_args = getattr(t, "tool_args", None)
            args = raw_args if isinstance(raw_args, dict) else None
            trace.append(
                ToolTraceEntry(
                    name=tname,
                    args=args,
                    result_preview=preview,
                    error=bool(getattr(t, "tool_call_error", False)),
                )
            )
        return trace
