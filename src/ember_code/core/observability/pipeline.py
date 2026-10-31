"""The OpenTelemetry SDK, set up for one collector.

Built only by :class:`~ember_code.core.observability.runtime.OtelRuntime`,
and only once :class:`~ember_code.core.observability.target.OtlpTarget`
has said there is a collector igni may reach. Nothing here is global: the
tracer and meter providers are handed to the instrumentations rather than
installed as OpenTelemetry's process-wide defaults, so a library that
traces by itself does not start exporting through igni's collector, and
stopping the pipeline leaves the process as it found it.
"""

from __future__ import annotations

from typing import ClassVar

from openinference.instrumentation import TraceConfig
from openinference.instrumentation.agno import AgnoInstrumentor
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter as GrpcMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HttpMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpSpanExporter,
)
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from pydantic import BaseModel, ConfigDict

from ember_code import __version__
from ember_code.core.config.schemas.telemetry import OtlpConfig
from ember_code.core.observability.content import ContentScrubbingExporter
from ember_code.core.observability.run_spans import RunSpanProcessor


class OtelSinks(BaseModel):
    """Where spans and metrics are written: the collector, or a test's memory."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    span_exporter: SpanExporter
    metric_reader: MetricReader

    #: How often metrics are pushed to the collector.
    METRIC_INTERVAL_MS: ClassVar[int] = 60_000

    @classmethod
    def otlp(cls, config: OtlpConfig) -> OtelSinks:
        """OTLP exporters for ``config``'s collector, and nothing else."""
        base = config.endpoint.rstrip("/")
        headers = dict(config.headers)
        if config.protocol == "grpc":
            insecure = base.lower().startswith("http://")
            spans: SpanExporter = GrpcSpanExporter(
                endpoint=base, headers=headers, insecure=insecure
            )
            metrics: MetricExporter = GrpcMetricExporter(
                endpoint=base, headers=headers, insecure=insecure
            )
        else:
            spans = HttpSpanExporter(endpoint=f"{base}/v1/traces", headers=headers)
            metrics = HttpMetricExporter(endpoint=f"{base}/v1/metrics", headers=headers)
        return cls(
            span_exporter=spans,
            metric_reader=PeriodicExportingMetricReader(
                metrics, export_interval_millis=cls.METRIC_INTERVAL_MS
            ),
        )


class OtelPipeline:
    """Providers, processors and instrumentations for one collector."""

    def __init__(self, config: OtlpConfig, sinks: OtelSinks) -> None:
        resource = Resource.create(
            {"service.name": config.service_name, "service.version": __version__}
        )
        self._meter_provider = MeterProvider(
            resource=resource, metric_readers=[sinks.metric_reader]
        )
        exporter = (
            sinks.span_exporter
            if config.capture_content
            else ContentScrubbingExporter(sinks.span_exporter)
        )
        self._tracer_provider = TracerProvider(resource=resource)
        self._tracer_provider.add_span_processor(
            RunSpanProcessor(self._meter_provider.get_meter("ember_code.observability"))
        )
        self._tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        self._capture_content = config.capture_content

    @property
    def tracer_provider(self) -> TracerProvider:
        return self._tracer_provider

    @property
    def meter_provider(self) -> MeterProvider:
        return self._meter_provider

    def trace_config(self) -> TraceConfig:
        """OpenInference's own switches: content hidden unless it is captured."""
        if self._capture_content:
            return TraceConfig()
        return TraceConfig(
            hide_inputs=True,
            hide_outputs=True,
            hide_input_messages=True,
            hide_output_messages=True,
            hide_input_images=True,
            hide_input_text=True,
            hide_output_text=True,
            hide_embedding_vectors=True,
            hide_prompts=True,
            hide_choices=True,
            hide_llm_invocation_parameters=True,
        )

    def instrument(self) -> None:
        """Agno's runs, model calls and tool calls, and httpx's requests.

        httpx carries the trace context (``traceparent``) to igni server and,
        through it, to the inference nodes and modules, so one trace follows
        a turn across the deployment.
        """
        AgnoInstrumentor().instrument(
            tracer_provider=self._tracer_provider, config=self.trace_config()
        )
        HTTPXClientInstrumentor().instrument(tracer_provider=self._tracer_provider)

    def force_flush(self) -> None:
        self._tracer_provider.force_flush()
        self._meter_provider.force_flush()

    def shutdown(self) -> None:
        """Stop instrumenting, export what is held, and close the exporters."""
        AgnoInstrumentor().uninstrument()
        HTTPXClientInstrumentor().uninstrument()
        self._tracer_provider.shutdown()
        self._meter_provider.shutdown()


__all__ = ["OtelPipeline", "OtelSinks"]
