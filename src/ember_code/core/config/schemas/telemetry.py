"""``telemetry.otlp`` — OpenTelemetry to the customer's own collector.

igni exports traces and metrics over OTLP only when an administrator
names a collector. There is no default endpoint: with none, nothing is
exported and the OpenTelemetry SDK is not set up at all
(``core/observability``).

**Managed-only.** Honoured from the managed-settings file, the verified
group policy pack and the environment, never from a user's or a project's
config or the command line: ``TelemetryTier`` in ``merge_plan.py`` puts the
block back to its defaults before the organisation tiers merge. A repository
that could set it could send its readers' traces to a host of its choosing;
a person who could set it could do the same with ``capture_content: true``.

The environment is the standard OpenTelemetry one, applied to the fields
the organisation left unset — a deployment usually starts igni with these
rather than writing a file, and the organisation's own value wins where it
set one:

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` — ``endpoint``;
* ``OTEL_EXPORTER_OTLP_PROTOCOL`` — ``protocol`` (``grpc`` or ``http/protobuf``);
* ``OTEL_EXPORTER_OTLP_HEADERS`` — ``headers``, as ``key=value,key2=value2``;
* ``OTEL_SERVICE_NAME`` — ``service_name``;
* ``IGNI_OTEL_CAPTURE_CONTENT`` — ``capture_content`` (``true`` turns it on).

Under ``air_gapped`` the endpoint must be inside the network
(:class:`~ember_code.core.network_policy.NetworkPolicy`), or nothing is
exported and a warning says why.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import ClassVar, Literal
from urllib.parse import unquote

from pydantic import BaseModel, Field, model_validator

OtlpProtocol = Literal["grpc", "http/protobuf"]

#: What the OTLP variables accept as "on".
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class OtlpConfig(BaseModel):
    """Where igni's traces and metrics go, if anywhere."""

    ENDPOINT_ENV: ClassVar[str] = "OTEL_EXPORTER_OTLP_ENDPOINT"
    PROTOCOL_ENV: ClassVar[str] = "OTEL_EXPORTER_OTLP_PROTOCOL"
    HEADERS_ENV: ClassVar[str] = "OTEL_EXPORTER_OTLP_HEADERS"
    SERVICE_NAME_ENV: ClassVar[str] = "OTEL_SERVICE_NAME"
    CAPTURE_CONTENT_ENV: ClassVar[str] = "IGNI_OTEL_CAPTURE_CONTENT"

    #: The collector's base URL, e.g. ``http://otel-collector.acme.internal:4318``.
    #: For ``http/protobuf`` igni appends ``/v1/traces`` and ``/v1/metrics``;
    #: for ``grpc`` it is the collector's address. Empty: nothing is exported.
    endpoint: str = ""
    protocol: OtlpProtocol = "http/protobuf"
    #: Sent with every export, for the collector's authentication.
    headers: dict[str, str] = Field(default_factory=dict)
    service_name: str = "igni"
    #: Prompts, responses, file content and tool arguments. Off: spans
    #: carry names, ids, counts, timings and outcomes only.
    capture_content: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint.strip())

    @model_validator(mode="after")
    def _from_environment(self) -> OtlpConfig:
        self.fill_from(os.environ)
        return self

    def fill_from(self, environ: Mapping[str, str]) -> None:
        """Take the OTLP variables for every field the configuration left unset."""
        defaults = OtlpConfig.model_construct()
        endpoint = environ.get(self.ENDPOINT_ENV, "").strip()
        if not self.endpoint and endpoint:
            self.endpoint = endpoint
        protocol = environ.get(self.PROTOCOL_ENV, "").strip()
        if self.protocol == defaults.protocol and protocol in ("grpc", "http/protobuf"):
            self.protocol = protocol  # type: ignore[assignment]  # narrowed by the membership check
        if not self.headers:
            self.headers = self.parse_headers(environ.get(self.HEADERS_ENV, ""))
        service_name = environ.get(self.SERVICE_NAME_ENV, "").strip()
        if self.service_name == defaults.service_name and service_name:
            self.service_name = service_name
        if environ.get(self.CAPTURE_CONTENT_ENV, "").strip().lower() in _TRUTHY:
            self.capture_content = True

    @staticmethod
    def parse_headers(raw: str) -> dict[str, str]:
        """``key=value,key2=value2``, values URL-decoded, as the OTLP spec writes them."""
        headers: dict[str, str] = {}
        for pair in raw.split(","):
            key, sep, value = pair.partition("=")
            if sep and key.strip():
                headers[key.strip().lower()] = unquote(value.strip())
        return headers


class TelemetryConfig(BaseModel):
    """The ``telemetry`` block (see the module docstring)."""

    otlp: OtlpConfig = Field(default_factory=OtlpConfig)


__all__ = ["OtlpConfig", "OtlpProtocol", "TelemetryConfig"]
