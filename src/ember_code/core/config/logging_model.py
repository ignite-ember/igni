"""Logging-wrapped OpenAI-compatible model.

Extracted from ``models.py``. Renamed from the private
``_LoggingModel`` to :class:`LoggingModel` because it's now a public
collaborator that :class:`OpenAILikeBuilder` instantiates. The old
underscore-prefixed name stays available as a re-export from
``models.py`` for existing tests.

Responsibilities:
* Log every ``invoke`` / ``ainvoke`` / ``invoke_stream`` /
  ``ainvoke_stream`` call to the dedicated LLM call log
  (via the injected :class:`LlmCallLogger`).
* Sanitize multimodal message content when the underlying model
  isn't vision-capable (via the injected :class:`MessageSanitizer`).
* Wrap ``process_response_stream`` / ``aprocess_response_stream``
  to interleave ``CustomEvent(event='tool_call_input_delta', ...)``
  as tool-call argument bytes stream in.

``vision`` moved from a class-level attribute that ``ModelRegistry``
poked in after construction to a real ``__init__`` keyword — kills
the ``model._vision = ...`` reach-in the audit called out.
"""

from __future__ import annotations

from typing import Any

from agno.models.openai.like import OpenAILike

from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.message_sanitizer import MessageSanitizer
from ember_code.core.config.model_stream import (
    _aemit_tool_arg_deltas,
    _emit_tool_arg_deltas,
)


class LoggingModel(OpenAILike):
    """Thin OpenAI-compatible model wrapper that logs, sanitizes,
    and interleaves progressive tool-arg deltas.

    Tool-arg streaming
    ------------------
    Agno's ``_parse_provider_response_delta`` passes
    ``choice_delta.tool_calls`` through on every stream chunk, and
    ``_populate_stream_data`` yields a ``ModelResponse`` with those
    deltas. But the agent-layer ``handle_model_response_chunk``
    only reads ``.content`` / ``.reasoning_content`` from delta
    chunks and never inspects ``.tool_calls`` — so partial tool
    arguments are silently accumulated in
    ``stream_data.response_tool_calls`` and only surface AFTER the
    whole tool call completes (as ``ToolCallStartedEvent``).

    That kills progressive rendering for tools whose value IS the
    argument shape — the visualizer sub-agent's
    ``visualize({spec: {...}})`` call being the driving case. We
    want the FE to render the spec as its tokens land.

    Fix: wrap ``process_response_stream`` /
    ``aprocess_response_stream`` (the polymorphic yield point) and
    interleave ``CustomEvent`` deltas per chunk.
    """

    # ``OpenAILike`` is a plain dataclass, so instance attributes
    # set here don't collide with any Pydantic model-field policing
    # — they're normal Python attributes.

    def __init__(
        self,
        *,
        logger: LlmCallLogger,
        sanitizer: MessageSanitizer | None = None,
        vision: bool = False,
        **openai_like_kwargs: Any,
    ) -> None:
        super().__init__(**openai_like_kwargs)
        self.vision = vision
        self._llm_logger = logger
        self._sanitizer = sanitizer or MessageSanitizer()

    # ── Invoke methods ────────────────────────────────────────────

    def invoke(self, *args, **kwargs):
        self._log_call("invoke", args, kwargs, stream=False)
        args = self._sanitizer.sanitize_if_needed(args, vision=self.vision)
        return super().invoke(*args, **kwargs)

    async def ainvoke(self, *args, **kwargs):
        self._log_call("ainvoke", args, kwargs, stream=False)
        args = self._sanitizer.sanitize_if_needed(args, vision=self.vision)
        return await super().ainvoke(*args, **kwargs)

    def invoke_stream(self, *args, **kwargs):
        self._log_call("invoke_stream", args, kwargs, stream=True)
        args = self._sanitizer.sanitize_if_needed(args, vision=self.vision)
        yield from super().invoke_stream(*args, **kwargs)

    async def ainvoke_stream(self, *args, **kwargs):
        self._log_call("ainvoke_stream", args, kwargs, stream=True)
        args = self._sanitizer.sanitize_if_needed(args, vision=self.vision)
        async for chunk in super().ainvoke_stream(*args, **kwargs):
            yield chunk

    # ── Response streaming ────────────────────────────────────────

    def process_response_stream(self, *args, **kwargs):
        yield from _emit_tool_arg_deltas(super().process_response_stream(*args, **kwargs))

    async def aprocess_response_stream(self, *args, **kwargs):
        async for ev in _aemit_tool_arg_deltas(super().aprocess_response_stream(*args, **kwargs)):
            yield ev

    # ── Private helpers ───────────────────────────────────────────

    def _log_call(
        self,
        method: str,
        args: tuple,
        kwargs: dict | None,
        *,
        stream: bool,
    ) -> None:
        """Emit one LLM-call log line via the injected logger."""
        n_messages = len(args[0]) if args else len((kwargs or {}).get("messages", []))
        url = getattr(self, "base_url", None) or "default"
        self._llm_logger.log_call(
            method,
            model_id=self.id,
            n_messages=n_messages,
            stream=stream,
            url=url,
        )
