"""``provider: anthropic`` — Claude over the native Messages API.

Every test here talks to :class:`~tests.anthropic_stub.StubMessagesApi`, a
real HTTP server on 127.0.0.1, so the ``anthropic`` SDK's own client
makes each request and the suite's egress guard would fail the test if
anything went anywhere else. Nothing reaches Anthropic; the one live
test is ``test_anthropic_live.py``.

What is covered: the config fields as a registry row carries them; the
builder the catalog picks, and an install without the SDK; every way a
key resolves; the base URL and its override; what the request body
carries (sampling, caching, thinking); images and PDFs as native
content blocks; a tool-call round trip through an agent igni builds,
streaming and not; a second run after the client is replaced; and a
whole igni turn under the egress guard, reaching no Agno host.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from agno.agent import Agent
from agno.media import File, Image

from ember_code.backend.model_http_client_manager import ModelHttpClientManager
from ember_code.cli.invocation import load_settings_from_options
from ember_code.cli.options import CliOptions
from ember_code.core.agents.schemas import AgentConstructorArgs
from ember_code.core.agno_telemetry import AgnoTelemetry
from ember_code.core.auth.credentials import Credentials, CredentialsStore
from ember_code.core.auth.http_auth import CloudTokenAuth
from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.model_entry import CLOUD_TOKEN_SENTINEL, ModelRegistryEntry
from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.provider_builders import AnthropicBuilder
from ember_code.core.config.provider_catalog import ProviderCatalog
from ember_code.core.config.settings import ModelsConfig, Settings
from ember_code.core.paths import CONFIG_DIR
from ember_code.core.session.core import Session
from tests.anthropic_stub import Reply, StubMessagesApi, ToolUse
from tests.egress_guard import EgressGuard, InternalNetwork

MODEL_ID = "claude-opus-5-5"

#: The smallest valid PNG: one transparent pixel.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def build(entry: ModelRegistryEntry, **kwargs: Any) -> Any:
    return AnthropicBuilder().build(
        entry, cloud_token=kwargs.pop("cloud_token", None), llm_logger=LlmCallLogger(), **kwargs
    )


def entry(url: str, **fields: Any) -> ModelRegistryEntry:
    return ModelRegistryEntry.model_validate(
        {"provider": "anthropic", "model_id": MODEL_ID, "url": url, "api_key": "sk-test"} | fields
    )


def agent(model: Any, tools: list[Any] | None = None) -> Agent:
    """An agent built the way igni builds one (telemetry off included)."""
    args = AgentConstructorArgs(
        name="claude-test",
        model=model,
        description="",
        instructions=None,
        tools=tools,
        num_history_runs=1,
        retries=0,
    )
    return Agent(**args.to_agno_kwargs())


@pytest.fixture(autouse=True)
def _no_ambient_anthropic_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK reads these when it is allowed to; the builder never lets it."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


# ── Config ────────────────────────────────────────────────────────


def test_a_registry_row_carries_the_anthropic_fields() -> None:
    settings = Settings(
        models=ModelsConfig(
            registry={
                "claude": {
                    "provider": "anthropic",
                    "model_id": MODEL_ID,
                    "url": "https://llm-gateway.bank.internal",
                    "api_key_env": "BANK_CLAUDE_KEY",
                    "context_window": 1_000_000,
                    "vision": True,
                    "temperature": 0.2,
                    "max_tokens": 32000,
                    "timeout": 300,
                    "thinking": {"type": "adaptive", "display": "summarized"},
                    "effort": "high",
                    "prompt_caching": False,
                }
            }
        )
    )
    row = ModelRegistry(settings)._resolve_entry("claude")

    assert row is not None
    assert row.provider == "anthropic"
    assert row.thinking == {"type": "adaptive", "display": "summarized"}
    assert row.effort == "high"
    assert row.prompt_caching is False
    assert ModelRegistry(settings).get_context_window("claude") == 1_000_000


def test_the_anthropic_fields_default_to_nothing_but_caching() -> None:
    row = ModelRegistryEntry(provider="anthropic", model_id=MODEL_ID)
    assert (row.thinking, row.effort, row.prompt_caching) == (None, None, True)


# ── The builder the catalog picks ─────────────────────────────────


def test_the_catalog_picks_the_anthropic_builder() -> None:
    catalog = ProviderCatalog()
    assert catalog.has("anthropic")
    assert isinstance(catalog.builder_for("anthropic"), AnthropicBuilder)
    assert "anthropic" in catalog.available_providers()


def test_the_registry_builds_claude_for_an_anthropic_row() -> None:
    from agno.models.anthropic import Claude

    settings = Settings(
        models=ModelsConfig(
            registry={"claude": {"provider": "anthropic", "model_id": MODEL_ID, "api_key": "k"}}
        )
    )
    model = ModelRegistry(settings).get_model("claude")
    assert isinstance(model, Claude)
    assert model.id == MODEL_ID


def test_the_provider_colon_model_syntax_reaches_it_too() -> None:
    from agno.models.anthropic import Claude

    model = ModelRegistry(Settings()).get_model(f"anthropic:{MODEL_ID}")
    assert isinstance(model, Claude)


def test_an_install_without_the_sdk_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK is imported when the builder is made, never at module top."""
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    for name in [m for m in sys.modules if m.startswith("agno.models.anthropic")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.delitem(sys.modules, "ember_code.core.config.logging_claude", raising=False)

    builder = AnthropicBuilder()
    assert builder.available is False
    with pytest.raises(RuntimeError, match="anthropic SDK is not installed"):
        build(entry("http://127.0.0.1:9"))

    catalog = ProviderCatalog()
    assert not catalog.has("anthropic")
    assert "openai_like" in catalog.available_providers()
    settings = Settings(
        models=ModelsConfig(registry={"c": {"provider": "anthropic", "model_id": MODEL_ID}})
    )
    with pytest.raises(ValueError, match="Unknown provider 'anthropic'"):
        ModelRegistry(settings).get_model("c")


# ── Keys ──────────────────────────────────────────────────────────


async def _headers_of_one_call(model: Any, stub: StubMessagesApi) -> dict[str, str]:
    await agent(model).arun("hi")
    return stub.messages[-1].headers


async def test_a_literal_key_is_sent_as_x_api_key() -> None:
    with StubMessagesApi(Reply(text="hello")) as stub:
        headers = await _headers_of_one_call(build(entry(stub.url)), stub)
    assert headers["x-api-key"] == "sk-test"
    assert "authorization" not in headers


async def test_a_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANK_CLAUDE_KEY", "sk-from-env")
    with StubMessagesApi(Reply(text="hello")) as stub:
        row = entry(stub.url, api_key=None, api_key_env="BANK_CLAUDE_KEY")
        headers = await _headers_of_one_call(build(row), stub)
    assert headers["x-api-key"] == "sk-from-env"


async def test_a_key_from_a_command() -> None:
    with StubMessagesApi(Reply(text="hello")) as stub:
        row = entry(stub.url, api_key=None, api_key_cmd="echo sk-from-cmd")
        headers = await _headers_of_one_call(build(row), stub)
    assert headers["x-api-key"] == "sk-from-cmd"


async def test_cloud_token_sends_the_current_token(tmp_path: Path) -> None:
    creds = tmp_path / "credentials.json"
    CredentialsStore(str(creds)).save(Credentials.new("tok-now", "ada@acme.test"))
    with StubMessagesApi(Reply(text="hello")) as stub:
        row = entry(stub.url, api_key=CLOUD_TOKEN_SENTINEL)
        model = build(row, cloud_token="tok-at-build", cloud_auth=CloudTokenAuth(str(creds)))
        headers = await _headers_of_one_call(model, stub)
    assert headers["authorization"] == "Bearer tok-now"
    assert headers["x-api-key"] == "tok-at-build"


async def test_no_key_is_said_out_loud_and_nothing_ambient_is_used(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With no key configured the SDK would read the machine's — or an
    ``ant`` profile, and the base URL it names. It is never given the
    chance."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-bearer")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://203.0.113.9")  # TEST-NET-3
    with StubMessagesApi(Reply(text="hello")) as stub, caplog.at_level(logging.WARNING):
        model = build(entry(stub.url, api_key=None))
        headers = await _headers_of_one_call(model, stub)
    assert "No API key" in caplog.text
    assert headers["x-api-key"] == "not-set"
    assert "authorization" not in headers


# ── Base URL ──────────────────────────────────────────────────────


def test_without_a_url_requests_go_to_anthropic() -> None:
    model = build(ModelRegistryEntry(provider="anthropic", model_id=MODEL_ID, api_key="k"))
    assert str(model.get_async_client().base_url).rstrip("/") == AnthropicBuilder.DEFAULT_BASE_URL


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
async def test_url_overrides_the_base_url(suffix: str) -> None:
    """A gateway's URL works written either way: the SDK adds ``/v1``."""
    with StubMessagesApi(Reply(text="through the gateway")) as stub:
        out = await agent(build(entry(stub.url + suffix))).arun("hi")
    assert [r.path for r in stub.messages] == ["/v1/messages"]
    assert "through the gateway" in str(out.content)


def test_url_with_a_path_prefix_keeps_it() -> None:
    row = entry("https://gw.bank.internal/anthropic/v1")
    assert AnthropicBuilder.base_url_for(row) == "https://gw.bank.internal/anthropic"


# ── The request ───────────────────────────────────────────────────


def add_numbers(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


async def test_the_request_carries_the_configured_fields() -> None:
    with StubMessagesApi(Reply(text="fine")) as stub:
        row = entry(
            stub.url,
            temperature=0.3,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            effort="low",
        )
        model = build(row)
        await agent(model, tools=[add_numbers]).arun("hi")
    body = stub.messages[0].body
    assert body["model"] == MODEL_ID
    assert body["max_tokens"] == 2048
    assert body["temperature"] == 0.3
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low"}
    # Prompt caching: the system prompt and the last tool are marked.
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert model.timeout == 60.0
    assert model.http_client.timeout.read == 60.0


async def test_prompt_caching_can_be_turned_off() -> None:
    with StubMessagesApi(Reply(text="fine")) as stub:
        model = build(entry(stub.url, prompt_caching=False))
        await agent(model, tools=[add_numbers]).arun("hi")
    body = stub.messages[0].body
    assert all("cache_control" not in block for block in body["system"])
    assert all("cache_control" not in tool for tool in body["tools"])
    assert "thinking" not in body


async def test_images_and_pdfs_are_native_content_blocks(tmp_path: Path) -> None:
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    with StubMessagesApi(Reply(text="seen")) as stub:
        model = build(entry(stub.url, vision=True))
        await agent(model).arun(
            "What is in these?",
            images=[Image(content=PNG_1PX, format="png")],
            files=[File(filepath=pdf, mime_type="application/pdf")],
        )
    content = stub.messages[0].body["messages"][0]["content"]
    kinds = {block["type"]: block for block in content}
    assert kinds["image"]["source"]["type"] == "base64"
    assert kinds["image"]["source"]["media_type"] == "image/png"
    assert kinds["document"]["source"] == {
        "type": "base64",
        "media_type": "application/pdf",
        "data": base64.b64encode(pdf.read_bytes()).decode(),
    }


# ── Tool calls through the agent loop ─────────────────────────────

TOOL_SCRIPT = (
    Reply(
        text="Let me add.",
        tool_use=ToolUse(id="toolu_01", name="add_numbers", input={"a": 2, "b": 3}),
    ),
    Reply(text="The sum is 5."),
)


def _tool_result(stub: StubMessagesApi) -> dict[str, Any]:
    second = stub.messages[1].body["messages"]
    blocks = [b for m in second if m["role"] == "user" for b in m["content"] if isinstance(b, dict)]
    return next(b for b in blocks if b.get("type") == "tool_result")


async def test_a_tool_call_round_trip() -> None:
    calls: list[tuple[int, int]] = []

    def add_numbers(a: int, b: int) -> str:
        """Add two numbers."""
        calls.append((a, b))
        return str(a + b)

    with StubMessagesApi(*TOOL_SCRIPT) as stub:
        out = await agent(build(entry(stub.url)), tools=[add_numbers]).arun("What is 2 + 3?")

    assert calls == [(2, 3)]
    assert len(stub.messages) == 2
    result = _tool_result(stub)
    assert result["tool_use_id"] == "toolu_01"
    assert "5" in str(result["content"])
    # The assistant's tool_use goes back with it, as the API requires.
    assistant = [m for m in stub.messages[1].body["messages"] if m["role"] == "assistant"][-1]
    assert any(b.get("type") == "tool_use" and b["id"] == "toolu_01" for b in assistant["content"])
    assert "The sum is 5." in str(out.content)


async def test_a_tool_call_round_trip_streaming() -> None:
    with StubMessagesApi(*TOOL_SCRIPT) as stub:
        model = build(entry(stub.url))
        text = ""
        async for event in agent(model, tools=[add_numbers]).arun("What is 2 + 3?", stream=True):
            if getattr(event, "event", "") == "RunContent" and isinstance(event.content, str):
                text += event.content

    assert all(r.body.get("stream") is True for r in stub.messages)
    assert len(stub.messages) == 2
    result = _tool_result(stub)
    assert result["tool_use_id"] == "toolu_01"
    assert "5" in str(result["content"])
    assert "The sum is 5." in text


async def test_a_second_run_after_the_client_is_replaced() -> None:
    """What the backend does after every run (``ModelHttpClientManager``)."""
    with StubMessagesApi(Reply(text="one"), Reply(text="two")) as stub:
        model = build(entry(stub.url))
        runner = agent(model)
        await runner.arun("first")
        await ModelHttpClientManager().close_and_replace(runner)  # type: ignore[arg-type]
        out = await runner.arun("second")
    assert len(stub.messages) == 2
    assert "two" in str(out.content)


# ── A whole igni turn, under the egress guard ─────────────────────


def _write_config(home: Path, url: str) -> None:
    config = home / CONFIG_DIR / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "air_gapped: true\n"
        "update_check_ttl: 0\n"
        "plugins:\n"
        "  default_marketplace: false\n"
        "models:\n"
        "  default: claude\n"
        "  registry:\n"
        "    claude:\n"
        "      provider: anthropic\n"
        f"      model_id: {MODEL_ID}\n"
        f"      url: {url}\n"
        "      api_key: stub-key\n"
    )


async def test_an_igni_turn_on_claude_reaches_only_the_configured_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing but ``/v1/messages`` on the configured host: no Agno host,
    no Anthropic host, nothing else outside the network."""
    monkeypatch.delenv("IGNI_EGRESS_ALLOW_HOSTS", raising=False)
    monkeypatch.delenv("IGNI_AIR_GAPPED", raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "true")  # an opt-in, which air_gapped overrules
    project = tmp_path / "project"
    project.mkdir()
    with StubMessagesApi(Reply(text="Nothing left the building.")) as stub:
        _write_config(Path.home(), stub.url)
        settings = load_settings_from_options(CliOptions(no_web=True))
        AgnoTelemetry.enforce(os.environ, air_gapped=settings.air_gapped)
        with EgressGuard(network=InternalNetwork()).watching() as log:
            session = Session(settings, project_dir=project)
            response = await session.handle_message("Say something.")

    assert "Nothing left the building." in response
    assert {r.path.split("?")[0] for r in stub.requests} <= {
        "/v1/messages",
        "/v1/messages/count_tokens",
    }
    assert stub.messages, "the turn never reached the model"
    assert log.attempts == [], log.report()
