"""A reply cut at its length limit is one billed call, not three.

B31: ``ModelReplyCut`` was retryable, and the retry was Agno's run-level
one (``Agent(retries=models.retries)``), which re-runs the whole run on
any exception. A reply cut at ``finish_reason=length`` was asked for
twice more with the same messages and the same ``max_tokens``: three
identical billed calls that each ran into the same limit (seen in the 4B
rerun, streamed and not).

The retry is now the model's, per call, where Agno asks whether an error
is worth retrying, and a length cut (or a provider's stop) says no. A
stream that ended with no ``finish_reason`` — the connection dropped — is
transient and still retried.

Each test serves real OpenAI-compatible replies from a local HTTP server
to the model ``ModelRegistry`` builds and counts the requests.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from agno.agent import Agent
from agno.exceptions import ModelProviderError
from agno.run.base import RunStatus

from ember_code.core.agents.builder import AgentBuilder
from ember_code.core.agents.schemas import AgentBuildContext, AgentDefinition
from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.reply_ending import ModelReplyCut, ReplyCutRetries
from ember_code.core.config.settings import Settings


class Server:
    """An OpenAI-compatible endpoint whose every reply ends with ``finish``."""

    def __init__(self, finish: str | None) -> None:
        self.requests = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's name
                server.requests += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body.get("stream"):
                    chunks: list[dict[str, Any]] = [
                        {"index": 0, "delta": {"role": "assistant", "content": "part"}}
                    ]
                    if finish:
                        chunks.append({"index": 0, "delta": {}, "finish_reason": finish})
                    data = (
                        "".join(
                            "data: "
                            + json.dumps(
                                {
                                    "id": "c",
                                    "object": "chat.completion.chunk",
                                    "created": 0,
                                    "model": "m",
                                    "choices": [{"finish_reason": None, **c}],
                                }
                            )
                            + "\n\n"
                            for c in chunks
                        )
                        + "data: [DONE]\n\n"
                    )
                    payload, kind = data.encode(), "text/event-stream"
                else:
                    payload = json.dumps(
                        {
                            "id": "c",
                            "object": "chat.completion",
                            "created": 0,
                            "model": "m",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "part"},
                                    "finish_reason": finish,
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ).encode()
                    kind = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[Server] = []

    def start(finish: str | None) -> Server:
        servers.append(Server(finish))
        return servers[-1]

    yield start
    for s in servers:
        s.close()


def _settings(url: str) -> Settings:
    settings = Settings()
    settings.models.registry = {
        "m": {
            "provider": "openai_like",
            "model_id": "m",
            "url": url,
            "api_key": "k",
            "max_tokens": 16,
        }
    }
    settings.models.retries = 2
    return settings


def _run(settings: Settings, *, stream: bool) -> RunStatus:
    model = ModelRegistry(settings).get_model("m")
    model.delay_between_retries = 0
    # As igni builds every agent: the model retries, the agent does not.
    agent = Agent(model=model, retries=0)

    async def go() -> RunStatus:
        if stream:
            events = [e async for e in agent.arun("hi", stream=True)]
            assert type(events[-1]).__name__ == "RunErrorEvent"
            return RunStatus.error
        return (await agent.arun("hi")).status

    return asyncio.run(go())


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
def test_a_length_cut_is_asked_for_once(serve, stream: bool):
    server = serve("length")
    assert _run(_settings(server.url), stream=stream) is RunStatus.error
    assert server.requests == 1


def test_a_dropped_stream_is_tried_again(serve):
    """No finish_reason: the connection dropped, which a retry can fix."""
    server = serve(None)
    assert _run(_settings(server.url), stream=True) is RunStatus.error
    assert server.requests == 3, "once, then models.retries more"


def test_a_finished_reply_is_one_call(serve):
    server = serve("stop")
    model = ModelRegistry(_settings(server.url)).get_model("m")
    response = asyncio.run(Agent(model=model, retries=0).arun("hi"))
    assert response.status is RunStatus.completed
    assert server.requests == 1


def test_the_registry_gives_the_model_its_retries(serve):
    model = ModelRegistry(_settings(serve("stop").url)).get_model("m")
    assert model.retries == 2
    assert isinstance(model, ReplyCutRetries)


def test_igni_builds_agents_without_run_level_retries():
    """Agno's run-level retry re-runs the whole run on any exception, with
    no test of whether it could help; the model's is the one that asks."""
    agent = AgentBuilder(AgentBuildContext(settings=Settings(), base_dir=".")).build(
        AgentDefinition(name="explorer", description="looks around")
    )
    assert agent.retries == 0


@pytest.mark.parametrize(
    ("cut", "retryable"),
    [
        (ModelReplyCut.at_length("m", 16), False),
        (ModelReplyCut.stopped("m", "content_filter"), False),
        (ModelReplyCut.no_ending("m"), True),
    ],
)
def test_which_cuts_are_worth_retrying(cut: ModelReplyCut, retryable: bool):
    checker = type("Checker", (ReplyCutRetries, _Base), {})()
    assert checker._is_retryable_error(cut) is retryable
    # As Agno's non-streamed invoke re-raises it: the cut is the cause.
    try:
        try:
            raise cut
        except ModelReplyCut as inner:
            raise ModelProviderError(str(inner)) from inner
    except ModelProviderError as wrapped:
        assert checker._is_retryable_error(wrapped) is retryable


class _Base:
    def _is_retryable_error(self, error: ModelProviderError) -> bool:
        return True
