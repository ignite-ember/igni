"""Anthropic and Gemini replies that did not finish end in an error too.

37b8389a made a cut reply an error for OpenAI-compatible servers only:
``LoggingModel`` read ``finish_reason``; Agno's ``Claude`` and ``Gemini``
read nothing of the kind, so a Claude reply stopped at ``max_tokens`` and
a Gemini one stopped by ``SAFETY`` or cut at ``MAX_TOKENS`` reached the
clients as a finished answer. These drive igni's model classes over the
providers' own SDK types, with the network call replaced by the events a
stream would carry, parsed the way Agno parses them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from agno.exceptions import ModelProviderError
from agno.models.anthropic import Claude
from agno.models.google import Gemini
from anthropic.types import (
    Message as AnthropicMessage,
)
from anthropic.types import (
    MessageDeltaUsage,
    RawMessageDeltaEvent,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta
from google.genai import types as genai

from ember_code.core.config.checked_gemini import CheckedGemini
from ember_code.core.config.logging_claude import LoggingClaude
from ember_code.core.config.model_entry import ModelRegistryEntry
from ember_code.core.config.provider_builders import GeminiBuilder
from ember_code.core.config.reply_ending import ModelReplyCut


def claude() -> LoggingClaude:
    return LoggingClaude(logger=MagicMock(), id="claude-x", api_key="k", max_tokens=1024)


def message_delta(stop_reason: str | None) -> RawMessageDeltaEvent:
    return RawMessageDeltaEvent(
        type="message_delta",
        delta=Delta(stop_reason=stop_reason, stop_sequence=None),  # type: ignore[arg-type]
        usage=MessageDeltaUsage(output_tokens=1),
    )


def anthropic_message(stop_reason: str | None) -> AnthropicMessage:
    return AnthropicMessage.model_construct(
        id="m",
        type="message",
        role="assistant",
        model="claude-x",
        content=[],
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def drain(model: Any, base: type, events: list[Any], parse_name: str) -> None:
    """Run ``model.invoke_stream`` over ``events``, Agno's parser stubbed out."""

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        for event in events:
            yield getattr(self, parse_name)(event)

    with (
        patch.object(base, "invoke_stream", invoke_stream),
        patch.object(base, parse_name, lambda self, event, *a, **k: event),
    ):
        list(model.invoke_stream([], MagicMock()))


class TestClaude:
    def test_a_reply_stopped_at_max_tokens_is_cut(self):
        with pytest.raises(ModelReplyCut) as cut:
            drain(claude(), Claude, [message_delta("max_tokens")], "_parse_provider_response_delta")
        assert "output length limit (1024 tokens)" in str(cut.value)
        # Not read as a context-window error, which would compact instead.
        assert ModelProviderError.CONTEXT_WINDOW_PATTERNS
        assert not any(
            p in str(cut.value).lower() for p in ModelProviderError.CONTEXT_WINDOW_PATTERNS
        )

    def test_a_stream_with_no_stop_reason_is_cut(self):
        with pytest.raises(ModelReplyCut, match="cut off"):
            drain(claude(), Claude, [message_delta(None)], "_parse_provider_response_delta")

    def test_a_refusal_is_a_stopped_reply(self):
        with pytest.raises(ModelReplyCut, match=r"stopped before it finished \(refusal\)"):
            drain(claude(), Claude, [message_delta("refusal")], "_parse_provider_response_delta")

    @pytest.mark.parametrize("reason", ["end_turn", "tool_use", "stop_sequence"])
    def test_a_finished_reply_passes(self, reason: str):
        drain(claude(), Claude, [message_delta(reason)], "_parse_provider_response_delta")

    def test_a_non_streamed_reply_at_max_tokens_is_cut(self):
        with pytest.raises(ModelReplyCut):
            claude()._parse_provider_response(anthropic_message("max_tokens"))


def gemini_chunk(reason: genai.FinishReason | None) -> genai.GenerateContentResponse:
    return genai.GenerateContentResponse(
        candidates=[
            genai.Candidate(
                content=genai.Content(role="model", parts=[genai.Part(text="hi")]),
                finish_reason=reason,
            )
        ]
    )


def gemini() -> CheckedGemini:
    return CheckedGemini(id="gemini-x", api_key="k", max_output_tokens=512)


class TestGemini:
    def test_a_reply_cut_at_max_tokens_is_cut(self):
        with pytest.raises(ModelReplyCut, match=r"length limit \(512 tokens\)"):
            drain(
                gemini(),
                Gemini,
                [gemini_chunk(None), gemini_chunk(genai.FinishReason.MAX_TOKENS)],
                "_parse_provider_response_delta",
            )

    def test_a_safety_stop_is_a_stopped_reply(self):
        with pytest.raises(ModelReplyCut, match=r"\(SAFETY\)"):
            drain(
                gemini(),
                Gemini,
                [gemini_chunk(genai.FinishReason.SAFETY)],
                "_parse_provider_response_delta",
            )

    def test_a_stream_with_no_finish_reason_is_cut(self):
        with pytest.raises(ModelReplyCut, match="cut off"):
            drain(gemini(), Gemini, [gemini_chunk(None)], "_parse_provider_response_delta")

    def test_a_finished_reply_passes(self):
        drain(
            gemini(),
            Gemini,
            [gemini_chunk(None), gemini_chunk(genai.FinishReason.STOP)],
            "_parse_provider_response_delta",
        )

    def test_a_non_streamed_reply_stopped_by_recitation_is_cut(self):
        with pytest.raises(ModelReplyCut, match="RECITATION"):
            gemini()._parse_provider_response(gemini_chunk(genai.FinishReason.RECITATION))

    def test_the_builder_builds_the_checked_class_with_its_cap(self):
        entry = ModelRegistryEntry(provider="gemini", model_id="gemini-x", max_tokens=512)
        built = GeminiBuilder().build(entry, cloud_token=None, llm_logger=MagicMock())
        assert isinstance(built, CheckedGemini)
        assert built.max_output_tokens == 512
