"""A model reply that did not finish ends the run in an error.

Run end to end against the real igni-1.0-4b through igni__server's chat
proxy, a stream that stopped with no ``finish_reason`` (the proxy's
keepalive closed it) and one cut at ``finish_reason=length`` (the model
reasoned through its whole 4,096-token budget) both reached the clients
as success: ``model_completed`` and ``run_completed`` with 0 tokens and an
empty reply, and no ``run_error``. Agno reads no ``finish_reason``.

These drive a real Agno agent on igni's :class:`LoggingModel`, with the
OpenAI client replaced by one that replays chunks, and read the run back
as the protocol messages the web client and the backend see.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from agno.agent import Agent
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from ember_code.core.config.logging_model import LoggingModel
from ember_code.core.config.settings import Settings
from ember_code.core.guardrails.runner import GuardrailRunner
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.session.message_handler import SessionMessageHandler
from ember_code.core.session.runner import run_single_message
from ember_code.protocol import messages as msg
from ember_code.protocol.serializer import serialize_event


def chunk(content: str | None = None, finish: str | None = None, **extra: Any):
    return ChatCompletionChunk.model_validate(
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "igni-1.0-4b",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ],
            **extra,
        }
    )


USAGE_ONLY = ChatCompletionChunk.model_validate(
    {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "igni-1.0-4b",
        "choices": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
)


def completion(content: str, finish: str) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "igni-1.0-4b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ],
        }
    )


class _Replay:
    def __init__(self, chunks: list[Any], pause: float = 0.0) -> None:
        self._chunks = chunks
        self._pause = pause

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._chunks:
            await asyncio.sleep(self._pause)
            yield item


def model(chunks: list[Any] | None = None, whole: Any = None, pause: float = 0.0) -> LoggingModel:
    m = LoggingModel(
        logger=MagicMock(),
        id="igni-1.0-4b",
        base_url="http://127.0.0.1:9/v1",
        api_key="unused",
        max_tokens=4096,
    )
    client = MagicMock()

    async def create(**kwargs: Any) -> Any:
        return _Replay(chunks or [], pause) if kwargs.get("stream") else whole

    client.chat.completions.create = create
    m.get_async_client = lambda: client  # type: ignore[method-assign]
    return m


async def streamed(agent: Agent) -> list[msg.Message]:
    out = []
    async for event in agent.arun("Say hello", stream=True, stream_events=True):
        proto = serialize_event(event)
        if proto is not None:
            out.append(proto)
    return out


def _types(messages: list[msg.Message]) -> list[str]:
    return [m.type for m in messages]


async def test_a_stream_that_ends_without_a_finish_reason_is_a_run_error():
    messages = await streamed(Agent(model=model([chunk("\n\n")])))

    assert "run_completed" not in _types(messages)
    assert "model_completed" not in _types(messages)
    (error,) = [m for m in messages if isinstance(m, msg.RunError)]
    assert "cut off" in error.error and "finish_reason" in error.error


async def test_a_reply_cut_at_the_length_limit_says_so():
    messages = await streamed(Agent(model=model([chunk("\n\n", "length")])))

    assert "run_completed" not in _types(messages)
    (error,) = [m for m in messages if isinstance(m, msg.RunError)]
    assert "output length limit (4096 tokens)" in error.error
    assert "finish_reason=length" in error.error


async def test_a_finished_reply_still_completes():
    messages = await streamed(Agent(model=model([chunk("Hi"), chunk(None, "stop"), USAGE_ONLY])))

    assert "run_error" not in _types(messages)
    assert _types(messages)[-1] == "run_completed"


async def test_two_replies_on_one_model_are_judged_apart():
    """A spawned agent is a shallow copy of the pool's and shares its
    model: a cut reply on one must not fail, or excuse, the other."""
    cut = model([chunk("a"), chunk("b"), chunk("c")], pause=0.01)
    finished = model([chunk("a"), chunk("b"), chunk(None, "stop")], pause=0.01)
    shared = cut
    # One model object, two streams interleaving on it.
    finished_client = finished.get_async_client()
    cut_client = cut.get_async_client()
    calls = iter([cut_client, finished_client])
    shared.get_async_client = lambda: next(calls)  # type: ignore[method-assign]

    first, second = await asyncio.gather(
        streamed(Agent(model=shared)), streamed(Agent(model=shared))
    )

    assert "run_error" in _types(first)
    assert "run_error" not in _types(second)
    assert _types(second)[-1] == "run_completed"


async def test_a_cut_non_streamed_reply_fails_the_headless_run():
    agent = Agent(model=model(whole=completion("\n\n", "length")))
    display = MagicMock()
    handler = SessionMessageHandler(
        hook_executor=HookExecutor({}),
        audit=MagicMock(),
        display=display,
        guardrail_runner=GuardrailRunner(Settings()),
        team_ref=lambda: agent,
        pending_reminders_drain=lambda: [],
        compact_hook=AsyncMock(return_value=False),
        ensure_mcp=AsyncMock(),
        session_id_ref=lambda: "s1",
        context_window_ref=lambda: 100_000,
        latch_input_tokens=lambda _tokens: None,
    )
    session = MagicMock()
    session.session_id = "s1"
    session.settings.models.default = "igni-1.0-4b"
    session.hook_executor = HookExecutor({})
    session.display = display
    session.plan_store = None
    session.handle_message = handler.handle
    type(session).last_turn_failure = PropertyMock(side_effect=lambda: handler.last_failure)

    with patch("ember_code.core.session.session_run.Session", return_value=session):
        try:
            await run_single_message(MagicMock(), "Say hello")
            code = 0
        except SystemExit as exit_:
            code = int(exit_.code or 0)

    assert code != 0
    assert "output length limit" in display.print_error.call_args.args[0]
    display.print_response.assert_not_called()
