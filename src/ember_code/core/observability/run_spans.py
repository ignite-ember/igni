"""igni's own span attributes, and the metrics read off finished spans.

OpenInference's Agno instrumentation opens an ``AGENT`` span per agent or
team run, an ``LLM`` span per model call and a ``TOOL`` span per tool call.
:class:`RunSpanProcessor` adds what only igni knows to them — whose run it
was (the organisation and producer a cloud backend serves, or the person
signed in) — and counts each finished span into three metrics:

* ``igni.agent.runs`` — runs, by agent or team and outcome, with
  ``igni.agent.run.duration`` beside it;
* ``igni.llm.tokens`` — tokens, by model and direction (``input`` /
  ``output``);
* ``igni.tool.calls`` — tool calls, by tool, outcome and permission
  decision.

An outcome is ``ok`` or ``error`` from the span's status, and a tool call
igni's permission pipeline refused is ``denied`` (its span carries
``igni.permission.decision``, set by :class:`~ember_code.core.observability.runtime.OtelRuntime`).
"""

from __future__ import annotations

import logging
from typing import ClassVar

from opentelemetry.context import Context
from opentelemetry.metrics import Meter
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import StatusCode

from ember_code.core.db.scope import tenancy

logger = logging.getLogger(__name__)


class SpanAttribute:
    """The span attribute names igni reads or writes."""

    KIND: ClassVar[str] = "openinference.span.kind"
    AGENT_NAME: ClassVar[str] = "agent.name"
    MODEL: ClassVar[str] = "llm.model_name"
    PROMPT_TOKENS: ClassVar[str] = "llm.token_count.prompt"
    COMPLETION_TOKENS: ClassVar[str] = "llm.token_count.completion"
    TOOL_NAME: ClassVar[str] = "tool.name"
    ORG: ClassVar[str] = "igni.org.id"
    PRODUCER: ClassVar[str] = "igni.producer.id"
    PERMISSION: ClassVar[str] = "igni.permission.decision"
    OUTCOME: ClassVar[str] = "igni.outcome"


class RunSpanProcessor(SpanProcessor):
    """Stamps run spans with their tenant, and counts finished spans."""

    def __init__(self, meter: Meter) -> None:
        self._runs = meter.create_counter(
            "igni.agent.runs", unit="{run}", description="Agent and team runs, by outcome"
        )
        self._run_duration = meter.create_histogram(
            "igni.agent.run.duration", unit="s", description="How long an agent or team run took"
        )
        self._tokens = meter.create_counter(
            "igni.llm.tokens", unit="{token}", description="Model tokens, by direction"
        )
        self._tool_calls = meter.create_counter(
            "igni.tool.calls", unit="{call}", description="Tool calls, by outcome"
        )

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if (span.attributes or {}).get(SpanAttribute.KIND) != "AGENT":
            return
        try:
            tenant = tenancy.tenant()
        except Exception as exc:  # noqa: BLE001 — a span must never fail a run
            logger.debug("no tenant for the run span: %s", exc)
            return
        if tenant is not None:
            span.set_attribute(SpanAttribute.ORG, tenant.org)
            span.set_attribute(SpanAttribute.PRODUCER, tenant.producer)

    def on_end(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        kind = attributes.get(SpanAttribute.KIND)
        outcome = "error" if span.status.status_code is StatusCode.ERROR else "ok"
        if kind == "AGENT":
            labels = {
                SpanAttribute.AGENT_NAME: str(attributes.get(SpanAttribute.AGENT_NAME, "")),
                SpanAttribute.OUTCOME: outcome,
            }
            self._runs.add(1, labels)
            if span.start_time is not None and span.end_time is not None:
                self._run_duration.record((span.end_time - span.start_time) / 1e9, labels)
        elif kind == "LLM":
            model = str(attributes.get(SpanAttribute.MODEL, ""))
            for direction, key in (
                ("input", SpanAttribute.PROMPT_TOKENS),
                ("output", SpanAttribute.COMPLETION_TOKENS),
            ):
                count = attributes.get(key)
                if isinstance(count, int) and count > 0:
                    self._tokens.add(
                        count, {SpanAttribute.MODEL: model, "igni.token.direction": direction}
                    )
        elif kind == "TOOL":
            decision = str(attributes.get(SpanAttribute.PERMISSION, ""))
            self._tool_calls.add(
                1,
                {
                    SpanAttribute.TOOL_NAME: str(attributes.get(SpanAttribute.TOOL_NAME, "")),
                    SpanAttribute.OUTCOME: "denied" if decision == "deny" else outcome,
                    SpanAttribute.PERMISSION: decision,
                },
            )

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


__all__ = ["RunSpanProcessor", "SpanAttribute"]
