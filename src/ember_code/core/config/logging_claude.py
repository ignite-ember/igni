"""Logging-wrapped Claude model: the ``anthropic`` provider's class.

:class:`LoggingModel` is this for OpenAI-compatible endpoints; this is
the same wrapper around Agno's native :class:`~agno.models.anthropic.Claude`,
which speaks the Messages API (``POST /v1/messages``) instead of a
chat-completions shim. It logs every call to the LLM call log and
flattens multimodal content for a model that is not ``vision``.

Importing this module imports the ``anthropic`` SDK, so only
:class:`~ember_code.core.config.provider_builders.AnthropicBuilder`
imports it, and only when asked to build one — an install without the
SDK still starts.

What it does not do is :class:`LoggingModel`'s progressive tool-argument
events. Agno's ``Claude`` reports a ``tool_use`` block once, whole, when
the block closes; it never surfaces the ``input_json_delta`` fragments in
between, so there is nothing to interleave.

Like :class:`LoggingModel`, it reads how each reply ended
(``reply_ending.py``): a ``stop_reason`` of ``max_tokens``, a ``refusal``,
or none at all is a :class:`ModelReplyCut`, not a finished empty answer.
"""

from __future__ import annotations

from typing import Any

from agno.models.anthropic import Claude

from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.message_sanitizer import MessageSanitizer
from ember_code.core.config.reply_ending import ANTHROPIC, ReplyCutRetries, ReplyEnding
from ember_code.core.config.tool_names import ToolNameBoundary


class LoggingClaude(ToolNameBoundary, ReplyCutRetries, Claude):
    """Agno's ``Claude``, logging each call and honouring ``vision``."""

    def __init__(
        self,
        *,
        logger: LlmCallLogger,
        sanitizer: MessageSanitizer | None = None,
        vision: bool = False,
        **claude_kwargs: Any,
    ) -> None:
        super().__init__(**claude_kwargs)
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
        ending = self._reply_ending()
        with ending.watching():
            yield from super().invoke_stream(*args, **kwargs)
        ending.check()

    async def ainvoke_stream(self, *args, **kwargs):
        self._log_call("ainvoke_stream", args, kwargs, stream=True)
        args = self._sanitizer.sanitize_if_needed(args, vision=self.vision)
        ending = self._reply_ending()
        with ending.watching():
            async for chunk in super().ainvoke_stream(*args, **kwargs):
                yield chunk
        ending.check()

    # ── How a reply ended ─────────────────────────────────────────

    def _parse_provider_response_delta(self, response, *args, **kwargs):
        ReplyEnding.note_chunk(response)
        return super()._parse_provider_response_delta(response, *args, **kwargs)

    def _parse_provider_response(self, response, *args, **kwargs):
        ReplyEnding.check_completion(response, self.id, self.max_tokens, ANTHROPIC)
        return super()._parse_provider_response(response, *args, **kwargs)

    def _reply_ending(self) -> ReplyEnding:
        return ReplyEnding(self.id, self.max_tokens, ANTHROPIC)

    # ── Private helpers ───────────────────────────────────────────

    @property
    def base_url(self) -> str:
        """Where requests go: the builder always names it."""
        return str((self.client_params or {}).get("base_url") or "default")

    def _log_call(
        self,
        method: str,
        args: tuple,
        kwargs: dict | None,
        *,
        stream: bool,
    ) -> None:
        n_messages = len(args[0]) if args else len((kwargs or {}).get("messages", []))
        self._llm_logger.log_call(
            method,
            model_id=self.id,
            n_messages=n_messages,
            stream=stream,
            url=self.base_url,
        )
