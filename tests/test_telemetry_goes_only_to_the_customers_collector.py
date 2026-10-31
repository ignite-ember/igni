"""igni's OpenTelemetry goes to the customer's collector, and only there.

``core/observability`` exports traces and metrics over OTLP. These tests
hold the three properties that let ``tests/test_nothing_phones_home.py``
allow the OTLP exporter packages at all:

* **no default endpoint** — with nothing configured, no provider is built,
  nothing is instrumented and nothing is exported;
* **customer-configured** — only the managed-settings file, the group
  policy and the ``OTEL_*`` environment name the collector; a user's or a
  repository's config cannot;
* **inside the network when air-gapped** — an external collector is refused.

And what is exported: real turns through igni's session, against a model
on 127.0.0.1, exported over OTLP/HTTP to a collector on 127.0.0.1 that
decodes what it receives. Spans carry names, ids, models, token counts and
outcomes — and, unless ``capture_content: true``, no prompt, response,
file content or tool argument.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import (
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
    Function,
)
from openinference.instrumentation.agno import AgnoInstrumentor
from opentelemetry import trace as otel_trace
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, Field

from ember_code.core.config.group_policy import GroupPolicyEntry, GroupPolicyPack
from ember_code.core.config.schemas.telemetry import OtlpConfig, TelemetryConfig
from ember_code.core.config.settings import Settings, SettingsLoader, load_settings
from ember_code.core.observability import OtelRuntime, OtlpTarget
from ember_code.core.observability.pipeline import OtelSinks
from ember_code.core.paths import CONFIG_DIR
from ember_code.core.session.core import Session
from tests.egress_guard import EgressGuard, InternalNetwork
from tests.test_igni_reaches_nothing_outside_the_network import StubModel

REPLY = "The notebook could not be read and the file was not created."
#: Written into the tool arguments; must never reach the collector by default.
SECRET = "CUSTOMER-SOURCE-7f3a"
PROMPT = f"Create notes.txt holding {SECRET}, then read analysis.ipynb."
MODEL_ID = "on-prem/model"

_OTEL_VARIABLES = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_SERVICE_NAME",
    "IGNI_OTEL_CAPTURE_CONTENT",
    "IGNI_AIR_GAPPED",
)


# ── A model that calls two tools, then answers ──────────────────────


class ToolCallingModel(StubModel):
    """Asks for ``create_file`` (denied by a rule below) and
    ``notebook_read`` (allowed) on its first turn, then answers in text."""

    CALLS: tuple[tuple[str, dict[str, str]], ...] = (
        ("create_file", {"file_path": "notes.txt", "content": SECRET}),
        ("notebook_read", {"file_path": "analysis.ipynb"}),
    )

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.requests = 0

    def completion(self, model: str) -> ChatCompletion:
        self.requests += 1
        if self.requests > 1:
            return super().completion(model)
        calls = [
            ChatCompletionMessageFunctionToolCall(
                id=f"call_{i}",
                type="function",
                function=Function(name=name, arguments=json.dumps(args)),
            )
            for i, (name, args) in enumerate(self.CALLS)
        ]
        return ChatCompletion(
            id="chatcmpl-tools",
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[
                Choice(
                    index=0,
                    finish_reason="tool_calls",
                    message=ChatCompletionMessage(role="assistant", tool_calls=calls),
                )
            ],
            usage=CompletionUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )

    def chunks(self, model: str) -> list[ChatCompletionChunk]:
        self.requests += 1
        if self.requests > 1:
            return super().chunks(model)

        def chunk(delta: ChoiceDelta, finish: str | None) -> ChatCompletionChunk:
            return ChatCompletionChunk(
                id="chatcmpl-tools",
                object="chat.completion.chunk",
                created=int(time.time()),
                model=model,
                choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish)],  # type: ignore[arg-type]
            )

        calls = [
            ChoiceDeltaToolCall(
                index=i,
                id=f"call_{i}",
                type="function",
                function=ChoiceDeltaToolCallFunction(name=name, arguments=json.dumps(args)),
            )
            for i, (name, args) in enumerate(self.CALLS)
        ]
        return [
            chunk(ChoiceDelta(role="assistant", tool_calls=calls), None),
            chunk(ChoiceDelta(), "tool_calls"),
        ]


# ── A collector on 127.0.0.1 ────────────────────────────────────────


class ExportedSpan(BaseModel):
    name: str
    trace_id: str
    attributes: dict[str, object]
    status_message: str = ""
    events: list[dict[str, object]] = Field(default_factory=list)


class OtlpCollector:
    """Receives OTLP/HTTP protobuf on ``/v1/traces`` and ``/v1/metrics``."""

    def __init__(self) -> None:
        self.spans: list[ExportedSpan] = []
        self.metrics: dict[str, list[tuple[dict[str, object], float]]] = {}
        self.headers: list[dict[str, str]] = []
        self.resource: dict[str, object] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectorHandler)
        self._server.collector = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> OtlpCollector:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def take_traces(self, body: bytes) -> None:
        request = ExportTraceServiceRequest.FromString(body)
        for resource_spans in request.resource_spans:
            self.resource = {a.key: _value(a.value) for a in resource_spans.resource.attributes}
            for scope in resource_spans.scope_spans:
                for span in scope.spans:
                    self.spans.append(
                        ExportedSpan(
                            name=span.name,
                            trace_id=span.trace_id.hex(),
                            attributes={a.key: _value(a.value) for a in span.attributes},
                            status_message=span.status.message,
                            events=[
                                {a.key: _value(a.value) for a in event.attributes}
                                for event in span.events
                            ],
                        )
                    )

    def take_metrics(self, body: bytes) -> None:
        request = ExportMetricsServiceRequest.FromString(body)
        for resource_metrics in request.resource_metrics:
            for scope in resource_metrics.scope_metrics:
                for metric in scope.metrics:
                    points = metric.sum.data_points or metric.histogram.data_points
                    for point in points:
                        labels = {a.key: _value(a.value) for a in point.attributes}
                        value = getattr(point, "as_int", 0) or getattr(point, "count", 0)
                        self.metrics.setdefault(metric.name, []).append((labels, value))

    def of_kind(self, kind: str) -> list[ExportedSpan]:
        return [s for s in self.spans if s.attributes.get("openinference.span.kind") == kind]


class _CollectorHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
        collector: OtlpCollector = self.server.collector  # type: ignore[attr-defined]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        collector.headers.append({k.lower(): v for k, v in self.headers.items()})
        if self.path == "/v1/traces":
            collector.take_traces(body)
        elif self.path == "/v1/metrics":
            collector.take_metrics(body)
        else:
            self.send_error(404)
            return
        reply = ExportTraceServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _value(value: AnyValue) -> object:
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [_value(v) for v in value.array_value.values]
    return getattr(value, kind) if kind else None


# ── Settings, the way a deployment writes them ──────────────────────


def write_user_config(home: Path, model_url: str, extra: str = "") -> None:
    config = home / CONFIG_DIR / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "update_check_ttl: 0\n"
        "plugins:\n"
        "  default_marketplace: false\n"
        "permissions:\n"
        "  file_write: allow\n"
        "  deny:\n"
        "    - create_file\n"
        "models:\n"
        "  default: on-prem\n"
        "  registry:\n"
        "    on-prem:\n"
        "      provider: openai_like\n"
        f"      model_id: {MODEL_ID}\n"
        f"      url: {model_url}\n"
        "      api_key: stub-key\n" + extra
    )


def managed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    managed = tmp_path / "managed-settings.yaml"
    managed.write_text(text)
    monkeypatch.setattr(SettingsLoader, "platform_managed_settings_path", lambda: managed)


def collector_block(url: str, *, capture_content: bool = False) -> str:
    return (
        "telemetry:\n"
        "  otlp:\n"
        f"    endpoint: {url}\n"
        "    headers:\n"
        "      authorization: Bearer collector-token\n"
        "    service_name: igni-test\n"
        f"    capture_content: {'true' if capture_content else 'false'}\n"
    )


async def one_turn(settings: Settings, project: Path) -> str:
    session = Session(settings, project_dir=project)
    return await session.handle_message(PROMPT)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for name in _OTEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    yield
    OtelRuntime.stop()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def agno_is_instrumented() -> bool:
    return bool(AgnoInstrumentor().is_instrumented_by_opentelemetry)


# ── No default endpoint ─────────────────────────────────────────────


class TestNothingIsSetUpWithoutACollector:
    async def test_with_no_endpoint_nothing_is_set_up(self, project, tmp_path):
        """No provider, no instrumentation, and the run reaches only its model."""
        settings = Settings()
        assert settings.telemetry.otlp.endpoint == ""

        started = OtelRuntime.start(settings)

        assert started.ok is False
        assert started.reason == ""
        assert OtelRuntime.current() is None
        assert not agno_is_instrumented()
        assert not isinstance(otel_trace.get_tracer_provider(), TracerProvider)

        with ToolCallingModel(REPLY) as model:
            write_user_config(Path.home(), model.url)
            with EgressGuard(network=InternalNetwork(), watch_subprocesses=True).watching() as log:
                await one_turn(load_settings(project_dir=project), project)
        assert not log.attempts, log.report()
        assert OtelRuntime.current() is None

    def test_the_default_block_names_no_collector(self):
        assert OtlpConfig.model_construct().endpoint == ""
        assert TelemetryConfig().otlp.enabled is False


# ── Customer-configured ─────────────────────────────────────────────


class TestOnlyTheOrganisationNamesTheCollector:
    def test_a_user_or_project_config_cannot_name_the_collector(self, project, caplog):
        (Path.home() / CONFIG_DIR).mkdir(parents=True, exist_ok=True)
        (Path.home() / CONFIG_DIR / "config.yaml").write_text(
            collector_block("http://user-chosen.example:4318", capture_content=True)
        )
        (project / CONFIG_DIR).mkdir()
        (project / CONFIG_DIR / "config.yaml").write_text(
            collector_block("http://repo-chosen.example:4318", capture_content=True)
        )

        otlp = load_settings(project_dir=project).telemetry.otlp

        assert otlp.endpoint == ""
        assert otlp.capture_content is False
        assert "telemetry may be set only in the managed-settings file" in caplog.text

    def test_the_command_line_cannot_name_it_either(self, project):
        cli = {"telemetry": {"otlp": {"endpoint": "http://cli-chosen.example:4318"}}}
        assert load_settings(project_dir=project, cli_overrides=cli).telemetry.otlp.endpoint == ""

    def test_the_managed_file_names_it(self, project, tmp_path, monkeypatch):
        managed_file(tmp_path, monkeypatch, collector_block("http://otel.acme.internal:4318"))
        otlp = load_settings(project_dir=project).telemetry.otlp

        assert otlp.endpoint == "http://otel.acme.internal:4318"
        assert otlp.headers == {"authorization": "Bearer collector-token"}
        assert otlp.service_name == "igni-test"

    def test_the_group_policy_names_it(self, project):
        pack = GroupPolicyPack(
            group_id="g",
            group_name="Platform",
            fetched_at="2026-10-31T00:00:00Z",
            entries=[
                GroupPolicyEntry(
                    kind="settings",
                    entry_name="otel",
                    content=json.dumps(
                        {"telemetry": {"otlp": {"endpoint": "http://otel.acme.internal:4318"}}}
                    ),
                    content_type="json",
                )
            ],
        )
        settings = load_settings(project_dir=project, group_policy_fetcher=lambda: pack)
        assert settings.telemetry.otlp.endpoint == "http://otel.acme.internal:4318"

    def test_the_environment_names_it(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel.acme.internal:4317")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20t,x-team=igni")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "igni-ci")

        otlp = Settings().telemetry.otlp

        assert otlp.endpoint == "http://otel.acme.internal:4317"
        assert otlp.protocol == "grpc"
        assert otlp.headers == {"authorization": "Bearer t", "x-team": "igni"}
        assert otlp.service_name == "igni-ci"
        assert otlp.capture_content is False

    def test_the_organisations_collector_wins_over_the_environment(
        self, project, tmp_path, monkeypatch
    ):
        """A shell variable is something anyone at the keyboard can set."""
        managed_file(tmp_path, monkeypatch, collector_block("http://otel.acme.internal:4318"))
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://elsewhere.example:4318")

        otlp = load_settings(project_dir=project).telemetry.otlp
        assert otlp.endpoint == "http://otel.acme.internal:4318"


# ── Inside the network when air-gapped ──────────────────────────────


def air_gapped_with(endpoint: str, **network: list[str]) -> Settings:
    return Settings(
        air_gapped=True,
        network={"internal_hosts": network.get("internal_hosts", [])},
        telemetry=TelemetryConfig(otlp=OtlpConfig(endpoint=endpoint)),
    )


def memory_sinks() -> tuple[OtelSinks, InMemorySpanExporter, InMemoryMetricReader]:
    spans = InMemorySpanExporter()
    metrics = InMemoryMetricReader()
    return OtelSinks(span_exporter=spans, metric_reader=metrics), spans, metrics


class TestTheCollectorIsInsideAnAirGappedNetwork:
    def test_an_external_collector_is_refused_when_air_gapped(self, caplog):
        settings = air_gapped_with("https://ingest.vendor-observability.example:4318")
        sinks, _, _ = memory_sinks()

        started = OtelRuntime.start(settings, sinks=sinks)

        assert started.ok is False
        assert "air-gapped" in started.reason
        assert "network.internal_hosts" in started.reason
        assert "air-gapped" in caplog.text
        assert OtelRuntime.current() is None
        assert not agno_is_instrumented()

    def test_a_collector_the_customer_named_is_allowed(self):
        settings = air_gapped_with(
            "http://otel.acme.internal:4318", internal_hosts=["*.acme.internal"]
        )
        sinks, _, _ = memory_sinks()
        assert OtelRuntime.start(settings, sinks=sinks).ok is True

    def test_a_collector_on_this_machine_is_allowed(self):
        sinks, _, _ = memory_sinks()
        assert OtelRuntime.start(air_gapped_with("http://127.0.0.1:4318"), sinks=sinks).ok

    def test_outside_the_air_gap_any_collector_the_organisation_names_is_used(self):
        target = OtlpTarget.resolve(
            Settings(telemetry=TelemetryConfig(otlp=OtlpConfig(endpoint="https://otel.acme.com")))
        )
        assert target.ok is True

    def test_an_endpoint_that_is_not_a_url_is_refused(self):
        target = OtlpTarget.resolve(
            Settings(telemetry=TelemetryConfig(otlp=OtlpConfig(endpoint="otel.acme.internal")))
        )
        assert target.ok is False
        assert "http://" in target.reason


# ── What reaches the collector ──────────────────────────────────────


class ExportedTurn(BaseModel):
    """One turn exported to the local collector."""

    model_config = {"arbitrary_types_allowed": True}

    response: str
    collector: OtlpCollector


async def exported_turn(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, capture_content: bool
) -> ExportedTurn:
    with OtlpCollector() as collector, ToolCallingModel(REPLY) as model:
        write_user_config(Path.home(), model.url)
        managed_file(
            tmp_path, monkeypatch, collector_block(collector.url, capture_content=capture_content)
        )
        settings = load_settings(project_dir=project)
        assert OtelRuntime.start(settings).ok
        response = await one_turn(settings, project)
        OtelRuntime.stop()
    return ExportedTurn(response=response, collector=collector)


def every_value(spans: list[ExportedSpan]) -> str:
    return json.dumps([[s.attributes, s.status_message, s.events] for s in spans], default=str)


class TestWhatIsExported:
    async def test_a_turn_is_exported_without_content(self, project, tmp_path, monkeypatch):
        turn = await exported_turn(project, tmp_path, monkeypatch, capture_content=False)
        collector = turn.collector

        assert REPLY in turn.response
        # The collector's own authentication went with every export.
        assert collector.headers
        assert all(h.get("authorization") == "Bearer collector-token" for h in collector.headers)
        assert collector.resource["service.name"] == "igni-test"

        # The run: who ran it, in which session.
        (run,) = [s for s in collector.of_kind("AGENT")]
        assert run.attributes["agent.name"]
        assert run.attributes["session.id"]
        # Model calls: the model and the tokens.
        llm = collector.of_kind("LLM")
        assert llm and all(s.attributes["llm.model_name"] == MODEL_ID for s in llm)
        assert any(s.attributes.get("llm.token_count.prompt") == 11 for s in llm)
        # Tool calls: by name, with igni's permission decision.
        tools = {s.attributes["tool.name"]: s for s in collector.of_kind("TOOL")}
        assert tools["create_file"].attributes["igni.permission.decision"] == "deny"
        assert tools["notebook_read"].attributes["igni.permission.decision"] == "allow"
        # One trace for the whole turn.
        assert len({s.trace_id for s in collector.of_kind("AGENT") + llm}) == 1

        # And no content: not the prompt, not the reply, not a tool argument.
        exported = every_value(collector.spans)
        for content in (SECRET, PROMPT, REPLY, "analysis.ipynb", "notes.txt"):
            assert content not in exported, f"{content!r} was exported"
        for key in ("input.value", "output.value", "tool.parameters"):
            assert all(key not in s.attributes for s in collector.spans), key
        assert all(
            not k.startswith(("llm.input_messages", "llm.output_messages"))
            for s in collector.spans
            for k in s.attributes
        )

    async def test_the_metrics_count_runs_tokens_and_tool_calls(
        self, project, tmp_path, monkeypatch
    ):
        metrics = (
            await exported_turn(project, tmp_path, monkeypatch, capture_content=False)
        ).collector.metrics

        runs = metrics["igni.agent.runs"]
        assert sum(v for _, v in runs) >= 1
        assert {labels["igni.outcome"] for labels, _ in runs} == {"ok"}
        tokens = {labels["igni.token.direction"]: v for labels, v in metrics["igni.llm.tokens"]}
        assert tokens["input"] >= 11 and tokens["output"] >= 7
        calls = {labels["tool.name"]: labels for labels, _ in metrics["igni.tool.calls"]}
        assert calls["create_file"]["igni.outcome"] == "denied"
        assert calls["notebook_read"]["igni.permission.decision"] == "allow"

    async def test_with_capture_content_the_content_is_exported(
        self, project, tmp_path, monkeypatch
    ):
        collector = (
            await exported_turn(project, tmp_path, monkeypatch, capture_content=True)
        ).collector

        exported = every_value(collector.spans)
        assert SECRET in exported
        assert "notebook_read" in exported
        tools = {s.attributes["tool.name"]: s for s in collector.of_kind("TOOL")}
        assert SECRET in str(tools["create_file"].attributes.get("tool.parameters"))

    async def test_stopping_leaves_the_process_as_it_was(self, project, tmp_path, monkeypatch):
        await exported_turn(project, tmp_path, monkeypatch, capture_content=False)

        assert OtelRuntime.current() is None
        assert not agno_is_instrumented()
        assert not isinstance(otel_trace.get_tracer_provider(), TracerProvider)
