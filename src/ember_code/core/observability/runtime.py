"""Starting and stopping igni's OpenTelemetry export, once per process.

:meth:`OtelRuntime.start` is called where a process first has its real
settings — ``BackendApp`` (the desktop and cloud backends) and the CLI's
``-m`` / pipe path. It does nothing unless
:class:`~ember_code.core.observability.target.OtlpTarget` finds a collector
igni may reach; it warns and does nothing if one is configured but the
``otel`` extra is not installed (``pip install 'ignite-ember[otel]'``).

The SDK modules are imported here behind ``OTEL_INSTALLED`` because they
belong to that optional extra: an install without it must still import
this module, and must still answer :meth:`OtelRuntime.note_permission`.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from ember_code.core.observability.target import OtlpTarget

try:
    from opentelemetry import trace as otel_trace

    from ember_code.core.observability.pipeline import OtelPipeline, OtelSinks
    from ember_code.core.observability.run_spans import SpanAttribute

    OTEL_INSTALLED = True
except ImportError:  # the ``otel`` extra is not installed
    OTEL_INSTALLED = False

logger = logging.getLogger(__name__)


class OtelStart(OtlpTarget):
    """What :meth:`OtelRuntime.start` did: started, or the reason it did not."""


class OtelRuntime:
    """The process's one export pipeline, or none.

    The pipeline is held on the class because OpenTelemetry's
    instrumentations are process-wide — Agno's run functions and httpx's
    transports are patched once for the whole process — so there is one to
    hold, and :meth:`stop` must find it from wherever the process ends.
    """

    _pipeline: ClassVar[OtelPipeline | None] = None

    @classmethod
    def start(cls, settings: object, *, sinks: OtelSinks | None = None) -> OtelStart:
        """Export to ``settings.telemetry.otlp``'s collector, if there is one igni may reach.

        ``sinks`` replaces the OTLP exporters, for tests; the endpoint is
        checked all the same.
        """
        target = OtlpTarget.resolve(settings)
        if not target.ok or target.config is None:
            if target.reason:
                logger.warning(target.reason)
            return OtelStart(ok=False, reason=target.reason)
        if not OTEL_INSTALLED:
            reason = (
                "telemetry.otlp.endpoint is set, but OpenTelemetry is not installed; "
                "install ignite-ember[otel] to export traces and metrics."
            )
            logger.warning(reason)
            return OtelStart(ok=False, reason=reason)
        cls.stop()
        pipeline = OtelPipeline(target.config, sinks or OtelSinks.otlp(target.config))
        pipeline.instrument()
        cls._pipeline = pipeline
        logger.info(
            "Exporting OpenTelemetry traces and metrics to %s (%s)%s.",
            target.config.endpoint,
            target.config.protocol,
            ", with content" if target.config.capture_content else "",
        )
        return OtelStart(ok=True, config=target.config)

    @classmethod
    def current(cls) -> OtelPipeline | None:
        return cls._pipeline

    @classmethod
    def flush(cls) -> None:
        if cls._pipeline is not None:
            cls._pipeline.force_flush()

    @classmethod
    def stop(cls) -> None:
        """Export what is held and stop instrumenting. Safe to call when not started."""
        pipeline, cls._pipeline = cls._pipeline, None
        if pipeline is not None:
            pipeline.shutdown()

    @classmethod
    def note_permission(cls, decision: str) -> None:
        """Record the permission pipeline's decision on the tool call's span.

        Called from inside the tool call (``core/hooks/tool_hook.py``), where
        the instrumentation's ``TOOL`` span is the current one. Does nothing
        when nothing is exported.
        """
        if cls._pipeline is None or not OTEL_INSTALLED:
            return
        span = otel_trace.get_current_span()
        if span.is_recording():
            span.set_attribute(SpanAttribute.PERMISSION, decision)


__all__ = ["OTEL_INSTALLED", "OtelRuntime", "OtelStart"]
