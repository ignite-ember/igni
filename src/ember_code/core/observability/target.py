"""Whether, and where, igni may export its traces and metrics.

Decided from settings alone, before any OpenTelemetry code runs: an
install with no collector, or with one it may not reach, never builds a
provider, an exporter or an instrumentation.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel

from ember_code.core.config.schemas.telemetry import OtlpConfig
from ember_code.core.network_policy import NetworkPolicy

#: The schemes an OTLP endpoint may use. ``http`` is plaintext, for a
#: collector on the same host or network segment; ``https`` is TLS.
_SCHEMES = frozenset({"http", "https"})


class OtlpTarget(BaseModel):
    """The outcome of :meth:`resolve`: a collector to export to, or why not."""

    ok: bool
    #: Why nothing is exported, when ``ok`` is false. Empty for the ordinary
    #: case of no endpoint configured, which is not worth a warning.
    reason: str = ""
    config: OtlpConfig | None = None

    @classmethod
    def resolve(cls, settings: object) -> OtlpTarget:
        """``settings.telemetry.otlp``, checked against the deployment's network.

        Typed as ``object`` for the same reason as
        :meth:`NetworkPolicy.from_settings`: session stubs hand a partial
        settings object, and one without a ``telemetry`` block exports nothing.
        """
        telemetry = getattr(settings, "telemetry", None)
        config = getattr(telemetry, "otlp", None)
        if not isinstance(config, OtlpConfig) or not config.enabled:
            return cls(ok=False)
        endpoint = config.endpoint.strip()
        parts = urlsplit(endpoint)
        if parts.scheme.lower() not in _SCHEMES or not parts.hostname:
            return cls(
                ok=False,
                reason=(
                    f"telemetry.otlp.endpoint {endpoint!r} is not an http:// or https:// URL, "
                    "so no traces or metrics are exported."
                ),
            )
        policy = NetworkPolicy.from_settings(settings)
        if not policy.allows(endpoint):
            return cls(
                ok=False,
                reason=policy.refusal(endpoint, what="the OpenTelemetry collector"),
            )
        return cls(ok=True, config=config.model_copy(update={"endpoint": endpoint}))


__all__ = ["OtlpTarget"]
