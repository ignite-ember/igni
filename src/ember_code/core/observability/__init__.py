"""OpenTelemetry traces and metrics, to the customer's own collector only.

Off by default, with no default endpoint. An administrator names a
collector in ``telemetry.otlp`` (managed settings, group policy or the
``OTEL_*`` environment; ``core/config/schemas/telemetry.py``); under
``air_gapped`` it must be inside the network. Then Agno's runs, model
calls and tool calls are traced through OpenInference's instrumentation,
httpx requests carry the trace context onward, and three metrics are
kept: runs, tokens and tool calls by outcome. Prompts, responses, file
content and tool arguments are not exported unless
``capture_content: true`` (``content.py``).

* ``target.py`` — whether there is a collector igni may reach;
* ``runtime.py`` — start and stop, once per process;
* ``pipeline.py`` — the SDK, set up for one collector;
* ``content.py`` — what a span may carry without content;
* ``run_spans.py`` — igni's attributes, and the metrics.

This is not Agno's own telemetry to ``os-api.agno.com``, which igni keeps
off (``core/agno_telemetry.py``).
"""

from ember_code.core.observability.runtime import OTEL_INSTALLED, OtelRuntime, OtelStart
from ember_code.core.observability.target import OtlpTarget

__all__ = ["OTEL_INSTALLED", "OtelRuntime", "OtelStart", "OtlpTarget"]
