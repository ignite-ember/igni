"""Provider catalog — replaces the ``ModelRegistry.PROVIDERS``
classvar (which was a mutable singleton pretending to be a
constant) with a per-``ModelRegistry`` instance.

The old code branched on provider names and looked up provider
classes in a class-level dict. The catalog holds
:class:`ProviderClientBuilder` instances instead of raw provider
classes, so polymorphism (each builder's ``build`` method) replaces
the string-branch dispatch that used to live in ``get_model``.
"""

from __future__ import annotations

from ember_code.core.config.provider_builders import (
    AnthropicBuilder,
    GeminiBuilder,
    OpenAILikeBuilder,
    ProviderClientBuilder,
)


class ProviderCatalog:
    """Registry of :class:`ProviderClientBuilder` keyed by provider
    name.

    Seeded with ``openai_like -> OpenAILikeBuilder`` and lazily
    seeds ``gemini -> GeminiBuilder`` and ``anthropic ->
    AnthropicBuilder`` on first lookup — matches the old
    ``ModelRegistry._load_provider`` behavior without the optional-dep
    pitfall of importing at module top.
    """

    #: Providers whose SDK is optional, probed on first lookup.
    _LAZY: dict[str, type[GeminiBuilder] | type[AnthropicBuilder]] = {
        "gemini": GeminiBuilder,
        "anthropic": AnthropicBuilder,
    }

    def __init__(self) -> None:
        self._builders: dict[str, ProviderClientBuilder] = {
            "openai_like": OpenAILikeBuilder(),
        }
        self._probed: set[str] = set()

    def register(self, name: str, builder: ProviderClientBuilder) -> None:
        """Register a custom builder (used by tests and third-party
        provider integrations)."""
        self._builders[name] = builder

    def has(self, name: str) -> bool:
        if name in self._builders:
            return True
        return self._probe(name)

    def builder_for(self, name: str) -> ProviderClientBuilder | None:
        """Return the builder for ``name`` or ``None`` when the
        provider is unknown / unavailable. Lazily probes an optional
        provider on first access, to avoid importing its SDK at module
        top."""
        builder = self._builders.get(name)
        if builder is not None:
            return builder
        if self._probe(name):
            return self._builders.get(name)
        return None

    def available_providers(self) -> list[str]:
        for name in self._LAZY:
            self._probe(name)
        return sorted(self._builders)

    def _probe(self, name: str) -> bool:
        builder_cls = self._LAZY.get(name)
        if builder_cls is None:
            return False
        if name in self._probed:
            return name in self._builders
        self._probed.add(name)
        builder = builder_cls()
        if builder.available:
            self._builders[name] = builder
            return True
        return False
