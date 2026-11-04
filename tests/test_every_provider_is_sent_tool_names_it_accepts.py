"""Every provider is sent tool names it accepts, and its calls come back by igni's name.

An extension's tool is registered as ``mcp_ext:acme.tickets_lookup_ticket``
— ``mcp_`` + the server (``ext:acme.tickets``) + ``_`` + the tool — and that
name went to the provider as it was. OpenAI's function names are
``^[a-zA-Z0-9_-]{1,64}$``, Anthropic's ``^[a-zA-Z0-9_-]{1,64}$`` (128 on
some models), Gemini's start with a letter or ``_``: a ``:`` or a ``.`` is
a 400 from each of them, so no extension tool could be offered to a
hosted model at all.

Names are now rewritten at the provider boundary only
(:mod:`ember_code.core.config.tool_names`) and the provider's calls are
mapped back before Agno looks the function up, so permissions, hints,
hooks and transcripts all keep the registered name. These run each
builder's model over its SDK with the network replaced, and read what
the SDK put on the wire.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
from agno.models.message import Message
from google.genai import types as genai

from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.model_entry import ModelRegistryEntry
from ember_code.core.config.provider_builders import (
    AnthropicBuilder,
    GeminiBuilder,
    OpenAILikeBuilder,
)
from ember_code.core.config.tool_names import ProviderToolNames, wire_name

REGISTERED = "mcp_ext:acme.tickets_lookup_ticket"
BUILT_IN = "read_file"
#: The strictest of the three: OpenAI's charset and length, Gemini's first character.
VALID = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,63}$")


def tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name}, for the test",
            "parameters": {
                "type": "object",
                "properties": {"ticket_id": {"type": "string"}},
                "required": ["ticket_id"],
            },
        },
    }


def history() -> list[Message]:
    """A conversation in which the model already called the tool once."""
    return [
        Message(role="user", content="look up T-1"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": REGISTERED, "arguments": '{"ticket_id": "T-0"}'},
                }
            ],
        ),
        Message(role="tool", content="{}", tool_call_id="call_0", tool_name=REGISTERED),
        Message(role="user", content="and T-1"),
    ]


def build(builder: Any, provider: str) -> Any:
    entry = ModelRegistryEntry(
        provider=provider, model_id="m", api_key="k", url="http://model.test/v1"
    )
    return builder.build(entry, cloud_token=None, llm_logger=LlmCallLogger())


class Wire:
    """What a mocked HTTP endpoint was sent."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def names_sent(self) -> set[str]:
        text = json.dumps(self.bodies)
        return set(re.findall(r'"name": "([^"]+)"', text))


async def test_openai_like_sends_valid_names_and_maps_the_call_back():
    wire = Wire()

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire.bodies.append(body)
        sent = body["tools"][0]["function"]["name"]
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": sent, "arguments": '{"ticket_id": "T-1"}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    model = build(OpenAILikeBuilder(), "openai_like")
    model.http_client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    response = await model.ainvoke(
        messages=history(),
        assistant_message=Message(role="assistant"),
        tools=[tool(REGISTERED), tool(BUILT_IN)],
    )

    names = wire.names_sent()
    assert names, "no tool names on the wire"
    assert all(VALID.match(n) for n in names), names
    assert BUILT_IN in names, "a name that was already valid is left alone"
    assert response.tool_calls[0]["function"]["name"] == REGISTERED


async def test_openai_like_maps_a_streamed_call_back():
    wire = Wire()

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire.bodies.append(body)
        sent = body["tools"][0]["function"]["name"]

        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            return "data: " + json.dumps(
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "m",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )

        events = [
            chunk(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": sent, "arguments": ""},
                        }
                    ],
                }
            ),
            chunk(
                {"tool_calls": [{"index": 0, "function": {"arguments": '{"ticket_id": "T-1"}'}}]}
            ),
            chunk({}, "tool_calls"),
            "data: [DONE]",
        ]
        return httpx.Response(
            200,
            text="\n\n".join(events) + "\n\n",
            headers={"content-type": "text/event-stream"},
        )

    model = build(OpenAILikeBuilder(), "openai_like")
    model.http_client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    seen: list[str] = []
    async for delta in model.ainvoke_stream(
        messages=history(),
        assistant_message=Message(role="assistant"),
        tools=[tool(REGISTERED)],
    ):
        for call in delta.tool_calls or []:
            fn = call.get("function") if isinstance(call, dict) else call.function
            name = (
                (fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None))
                if fn
                else None
            )
            if name:
                seen.append(name)

    assert all(VALID.match(n) for n in wire.names_sent())
    assert seen == [REGISTERED]
    # Agno assembles the streamed fragments with ``parse_tool_calls``.
    from openai.types.chat.chat_completion_chunk import (
        ChoiceDeltaToolCall,
        ChoiceDeltaToolCallFunction,
    )

    fragment = ChoiceDeltaToolCall(
        index=0,
        id="call_2",
        type="function",
        function=ChoiceDeltaToolCallFunction(name=wire_name(REGISTERED), arguments="{}"),
    )
    assert model.parse_tool_calls([fragment])[0]["function"]["name"] == REGISTERED


async def test_anthropic_sends_valid_names_and_maps_the_call_back():
    # The anthropic SDK is built on httpx2 and takes only its client.
    import httpx2

    wire = Wire()

    def answer(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        wire.bodies.append(body)
        sent = body["tools"][0]["name"]
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": sent, "input": {"ticket_id": "T-1"}}
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    model = build(AnthropicBuilder(), "anthropic")
    model.http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(answer))
    response = await model.ainvoke(
        messages=history(),
        assistant_message=Message(role="assistant"),
        tools=[tool(REGISTERED), tool(BUILT_IN)],
    )

    names = wire.names_sent()
    assert REGISTERED not in names
    assert all(VALID.match(n) for n in names), names
    assert response.tool_calls[0]["function"]["name"] == REGISTERED


async def test_gemini_sends_valid_names_and_maps_the_call_back():
    model = build(GeminiBuilder(), "gemini")
    sent: dict[str, Any] = {}

    async def generate_content(**kwargs: Any) -> genai.GenerateContentResponse:
        sent.update(kwargs)
        declared = kwargs["config"].tools[0].function_declarations[0].name
        return genai.GenerateContentResponse(
            candidates=[
                genai.Candidate(
                    content=genai.Content(
                        role="model",
                        parts=[
                            genai.Part(
                                function_call=genai.FunctionCall(
                                    name=declared, args={"ticket_id": "T-1"}
                                )
                            )
                        ],
                    ),
                    finish_reason=genai.FinishReason.STOP,
                )
            ]
        )

    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=generate_content)
    model.get_client = lambda: client  # type: ignore[method-assign]

    response = await model.ainvoke(
        messages=history(),
        assistant_message=Message(role="assistant"),
        tools=[tool(REGISTERED), tool(BUILT_IN)],
    )

    declared = {d.name for t in sent["config"].tools for d in t.function_declarations}
    in_history = {
        (part.function_call or part.function_response).name
        for content in sent["contents"]
        for part in content.parts
        if part.function_call or part.function_response
    }
    assert declared | in_history
    assert all(VALID.match(n) for n in declared | in_history), declared | in_history
    assert response.tool_calls[0]["function"]["name"] == REGISTERED


# ── The mapping ────────────────────────────────────────────────────


def test_the_extension_tool_s_wire_name():
    assert wire_name(REGISTERED) == "mcp_ext_acme_tickets_lookup_ticket"


def test_a_valid_name_is_its_own_wire_name():
    names = ProviderToolNames()
    assert names.assign([BUILT_IN, "mcp_github_search"]) == {
        BUILT_IN: BUILT_IN,
        "mcp_github_search": "mcp_github_search",
    }


def test_two_names_that_would_collide_do_not():
    names = ProviderToolNames()
    taken = "mcp_ext_acme_tickets_lookup_ticket"
    mapping = names.assign([REGISTERED, taken, "mcp_ext:acme.tickets.lookup_ticket"])
    assert mapping[taken] == taken
    assert len(set(mapping.values())) == 3
    assert all(VALID.match(v) for v in mapping.values())
    for original, wire in mapping.items():
        assert names.registered(wire) == original


def test_a_long_name_is_cut_to_64_with_a_hash_and_is_stable():
    long = "mcp_ext:com.example.a.very.long.extension.identifier_" + "x" * 80
    first = ProviderToolNames().assign([long])[long]
    second = ProviderToolNames().assign([long])[long]
    assert first == second
    assert len(first) <= 64 and VALID.match(first)


def test_a_name_starting_with_a_digit_gets_a_valid_first_character():
    assert VALID.match(wire_name("3d.render"))


def test_a_name_the_model_made_up_comes_back_as_it_was():
    assert ProviderToolNames().registered("no_such_tool") == "no_such_tool"


async def test_an_agent_runs_the_extension_tool_the_model_called_by_its_wire_name():
    """End to end through Agno: the call is dispatched to the registered function."""
    from agno.agent import Agent
    from agno.tools.function import Function

    ran: list[str] = []

    def lookup_ticket(ticket_id: str) -> str:
        ran.append(ticket_id)
        return f"ticket {ticket_id}: VPN drops every hour"

    requests: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            sent = body["tools"][0]["function"]["name"]
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": sent, "arguments": '{"ticket_id": "T-1"}'},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "It is about the VPN."}
            finish = "stop"
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    model = build(OpenAILikeBuilder(), "openai_like")
    model.http_client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    agent = Agent(model=model, tools=[Function.from_callable(lookup_ticket, name=REGISTERED)])

    run = await agent.arun("look up T-1")

    assert ran == ["T-1"]
    assert run.tools and run.tools[0].tool_name == REGISTERED
    # The second request carries the history: the call and its result, by wire name.
    second = json.dumps(requests[1])
    assert REGISTERED not in second
    assert wire_name(REGISTERED) in second
