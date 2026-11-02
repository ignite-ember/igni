"""One live call through ``provider: anthropic``, to Anthropic's own API.

``test_anthropic_provider.py`` covers the provider against a stub; this
checks the stub has not drifted from the real thing: a turn with a tool
call, streamed, through the builder igni uses. Skips unless
``ANTHROPIC_API_KEY`` is set, since every run costs money.
"""

from __future__ import annotations

import os

import pytest
from agno.agent import Agent

from ember_code.core.agents.schemas import AgentConstructorArgs
from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.model_entry import ModelRegistryEntry
from ember_code.core.config.provider_builders import AnthropicBuilder

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set (export it to run the live Anthropic test)",
    ),
    pytest.mark.reaches_the_internet,
]


async def test_a_streamed_tool_call_against_anthropic() -> None:
    calls: list[tuple[int, int]] = []

    def add_numbers(a: int, b: int) -> str:
        """Add two integers and return the sum."""
        calls.append((a, b))
        return str(a + b)

    entry = ModelRegistryEntry(
        provider="anthropic",
        model_id="claude-opus-5-5",
        api_key_env="ANTHROPIC_API_KEY",
        max_tokens=4096,
        effort="low",
    )
    model = AnthropicBuilder().build(entry, cloud_token=None, llm_logger=LlmCallLogger())
    args = AgentConstructorArgs(
        name="live-claude",
        model=model,
        description="",
        instructions="Use the add_numbers tool for arithmetic, then state the result.",
        tools=[add_numbers],
        num_history_runs=1,
        retries=0,
    )
    text = ""
    async for event in Agent(**args.to_agno_kwargs()).arun("What is 1234 + 4321?", stream=True):
        if getattr(event, "event", "") == "RunContent" and isinstance(event.content, str):
            text += event.content

    assert calls == [(1234, 4321)]
    assert "5555" in text.replace(",", "")
