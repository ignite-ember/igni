"""What a span may carry to the collector when content is not captured.

OpenInference's Agno instrumentation records a great deal by default:
the run's input and output (``input.value`` / ``output.value``), every
message sent to and received from the model (``llm.input_messages.*``,
``llm.output_messages.*``, tool-call arguments among them), each tool
call's arguments twice (``input.value`` and ``tool.parameters``) and its
result, and a failed tool's error text as the span status. Its
``TraceConfig`` hides most of that, but not ``tool.parameters`` and not
the status text, and a later version could add a field it does not know.

So with ``capture_content: false`` (the default) an exported span keeps
only the attributes named here — names, ids, models, token counts,
outcomes and igni's own ``igni.*`` — and everything else is dropped on
the way out: an **allow-list**, so an attribute nobody has read yet is
not exported. Exception events keep their type and lose their message and
stack trace, which quote whatever failed. ``TraceConfig`` is still set to
hide content too (``runtime.py``), so the content is not even held in
memory on the span.

With ``capture_content: true`` spans are exported as recorded.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status
from opentelemetry.util.types import Attributes


class SpanContentPolicy:
    """The attributes a span keeps when content is not captured."""

    #: Kept by exact name.
    KEEP: ClassVar[frozenset[str]] = frozenset(
        {
            # What the span is, and who ran it.
            "openinference.span.kind",
            "agent.name",
            "graph.node.id",
            "graph.node.name",
            "graph.node.parent_id",
            "agno.agent",
            "agno.agent.id",
            "agno.team",
            "agno.team.id",
            "agno.run.id",
            "agno.tools",
            "session.id",
            "user.id",
            # The model.
            "llm.model_name",
            "llm.provider",
            "llm.system",
            "gen_ai.operation.name",
            "gen_ai.provider.name",
            "gen_ai.system",
            "gen_ai.request.model",
            "gen_ai.response.model",
            "gen_ai.agent.name",
            # The tool, by name only.
            "tool.name",
            "gen_ai.tool.name",
            # An HTTP call, without its URL: a query string can carry a key.
            "http.method",
            "http.status_code",
            "http.request.method",
            "http.response.status_code",
            "server.address",
            "server.port",
            "url.scheme",
            "network.protocol.version",
            "error.type",
            # An exception event, by type.
            "exception.type",
        }
    )
    #: Kept by prefix: counts, costs and igni's own attributes.
    KEEP_PREFIXES: ClassVar[tuple[str, ...]] = (
        "igni.",
        "llm.token_count.",
        "llm.cost.",
        "gen_ai.usage.",
    )

    @classmethod
    def keeps(cls, key: str) -> bool:
        return key in cls.KEEP or key.startswith(cls.KEEP_PREFIXES)

    @classmethod
    def attributes(cls, attributes: Attributes) -> dict[str, object]:
        return {k: v for k, v in (attributes or {}).items() if cls.keeps(k)}

    @classmethod
    def scrub(cls, span: ReadableSpan) -> ReadableSpan:
        """A copy of ``span`` with only what :meth:`keeps` allows."""
        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=cls.attributes(span.attributes),  # type: ignore[arg-type]
            events=[
                Event(
                    name=event.name,
                    attributes=cls.attributes(event.attributes),  # type: ignore[arg-type]
                    timestamp=event.timestamp,
                )
                for event in span.events
                if event.name == "exception"
            ],
            links=span.links,
            kind=span.kind,
            # The code stays; the description is the failure's own text.
            status=Status(span.status.status_code),
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )


class ContentScrubbingExporter(SpanExporter):
    """Hands the wrapped exporter scrubbed copies (:class:`SpanContentPolicy`)."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([SpanContentPolicy.scrub(span) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


__all__ = ["ContentScrubbingExporter", "SpanContentPolicy"]
