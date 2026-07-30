"""Polymorphic provider client builders.

Extracted from ``models.py`` — replaces the string-branching
``if provider_name == 'gemini'`` dispatch AND the two ad-hoc
``_build_*_kwargs`` methods that shared no interface. Each builder
subclass owns its own kwarg surface and its own instantiation path.

The audit's "dispatch dict of free functions" offender is addressed
by polymorphism: :class:`ProviderCatalog` holds
``dict[str, ProviderClientBuilder]``, and each subclass overrides
:meth:`ProviderClientBuilder.build`.

``GeminiBuilder`` defers its ``agno.models.google`` import to
construction time inside a try/except and caches the class on the
instance — so importing this module never fails on installs that
lack ``google-genai``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.logging_model import LoggingModel
from ember_code.core.config.model_entry import ModelRegistryEntry


class ProviderClientBuilder(ABC):
    """Abstract builder — turns a :class:`ModelRegistryEntry` into
    an Agno-compatible model instance.

    Concrete subclasses own their kwarg construction. Passing the
    cloud token in at ``build`` time keeps the entry a pure data
    type — the entry doesn't reach into :class:`CloudCredentials`.
    """

    @abstractmethod
    def build(
        self,
        entry: ModelRegistryEntry,
        *,
        cloud_token: str | None,
        llm_logger: LlmCallLogger,
    ) -> Any:
        """Return a live Agno model instance for ``entry``."""


class OpenAILikeBuilder(ProviderClientBuilder):
    """Builder for OpenAI-compatible providers.

    Owns the base_url / api_key / temperature / max_tokens /
    timeout kwarg construction AND the ``httpx.AsyncClient`` wiring
    AND wrapping the resulting model in :class:`LoggingModel`.
    """

    def build(
        self,
        entry: ModelRegistryEntry,
        *,
        cloud_token: str | None,
        llm_logger: LlmCallLogger,
    ) -> LoggingModel:
        kwargs = self._build_kwargs(entry, cloud_token=cloud_token)
        return LoggingModel(
            logger=llm_logger,
            vision=entry.vision,
            **kwargs,
        )

    def _build_kwargs(
        self,
        entry: ModelRegistryEntry,
        *,
        cloud_token: str | None,
    ) -> dict[str, Any]:
        """OpenAI-like providers share a broad kwarg surface:
        base_url, api_key, temperature, max_tokens, timeout +
        http_client.

        Models with explicit credentials use them directly.
        Otherwise, authenticated users route through the Ember
        Cloud gateway. URL and API key resolve independently:
        - URL: from the entry, or the Ember Cloud gateway fallback
        - Key: from the entry, or the injected cloud token
        """
        kwargs: dict[str, Any] = {"id": entry.model_id}

        if entry.url:
            kwargs["base_url"] = entry.url

        api_key = entry.resolve_api_key(cloud_token=cloud_token)
        # OpenAILike requires SOME api_key value — even a placeholder
        # so the SDK's None-check inside its constructor doesn't
        # raise before the first call. When the resolver returns
        # None (missing env var, cmd failure, no cloud token) we
        # use the same "not-set" sentinel the old code used.
        kwargs["api_key"] = api_key or "not-set"

        if entry.temperature is not None:
            kwargs["temperature"] = entry.temperature
        if entry.max_tokens is not None:
            kwargs["max_tokens"] = entry.max_tokens

        # Request timeout — prevents indefinite hangs when the
        # server or upstream provider stops responding. Applied on
        # BOTH the OpenAI-SDK kwarg AND the underlying
        # ``httpx.AsyncClient`` — without both, the SDK-level
        # timeout is shadowed by httpx's defaults and hung
        # connections can wedge forever.
        timeout_s = entry.timeout
        kwargs["timeout"] = timeout_s

        # Short keepalive expiry avoids stale connections that
        # hang when reused after idle periods (e.g. between user
        # messages).
        kwargs["http_client"] = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30,
            ),
        )
        return kwargs


class GeminiBuilder(ProviderClientBuilder):
    """Builder for the Gemini provider.

    Gemini uses its own SDK with a slimmer kwarg surface than
    OpenAI-like providers (no ``base_url``, no ``http_client``).

    The ``agno.models.google.Gemini`` import is done lazily inside
    ``__init__`` — a bare import at module top would crash the
    whole ``provider_builders`` module on installs without the
    ``google-genai`` extra.
    """

    def __init__(self) -> None:
        self._gemini_cls: type | None = None
        try:
            from agno.models.google import Gemini  # noqa: WPS433 — lazy optional dep

            self._gemini_cls = Gemini
        except ImportError:
            self._gemini_cls = None

    @property
    def available(self) -> bool:
        return self._gemini_cls is not None

    def build(
        self,
        entry: ModelRegistryEntry,
        *,
        cloud_token: str | None,
        llm_logger: LlmCallLogger,
    ) -> Any:
        if self._gemini_cls is None:
            raise RuntimeError(
                "Gemini provider requested but google-genai is not installed. "
                "Run: pip install google-genai"
            )
        # ``llm_logger`` is intentionally unused for Gemini — the
        # Gemini SDK is not OpenAI-shaped so the ``LoggingModel``
        # wrapper doesn't fit its ``ainvoke`` / ``ainvoke_stream``
        # signature. Argument kept in the signature so the ABC
        # stays uniform across builders.
        _ = llm_logger
        kwargs = self._build_kwargs(entry, cloud_token=cloud_token)
        return self._gemini_cls(**kwargs)

    def _build_kwargs(
        self,
        entry: ModelRegistryEntry,
        *,
        cloud_token: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"id": entry.model_id}
        api_key = entry.resolve_api_key(cloud_token=cloud_token)
        if api_key:
            kwargs["api_key"] = api_key
        if entry.temperature is not None:
            kwargs["temperature"] = entry.temperature
        if entry.max_tokens is not None:
            kwargs["max_tokens"] = entry.max_tokens
        return kwargs
