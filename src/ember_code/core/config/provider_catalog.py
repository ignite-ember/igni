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
    GeminiBuilder,
    OpenAILikeBuilder,
    ProviderClientBuilder,
)


class ProviderCatalog:
    """Registry of :class:`ProviderClientBuilder` keyed by provider
    name.

    Seeded with ``openai_like -> OpenAILikeBuilder`` and lazily
    seeds ``gemini -> GeminiBuilder`` on first lookup — matches the
    old ``ModelRegistry._load_provider`` behavior without the
    optional-dep pitfall of importing at module top.
    """

    def __init__(self) -> None:
        self._builders: dict[str, ProviderClientBuilder] = {
            "openai_like": OpenAILikeBuilder(),
        }
        self._gemini_probed = False

    def register(self, name: str, builder: ProviderClientBuilder) -> None:
        """Register a custom builder (used by tests and third-party
        provider integrations)."""
        self._builders[name] = builder

    def has(self, name: str) -> bool:
        if name in self._builders:
            return True
        if name == "gemini":
            return self._probe_gemini()
        return False

    def builder_for(self, name: str) -> ProviderClientBuilder | None:
        """Return the builder for ``name`` or ``None`` when the
        provider is unknown / unavailable. Lazily probes Gemini on
        first access to avoid importing ``google-genai`` at module
        top."""
        builder = self._builders.get(name)
        if builder is not None:
            return builder
        if name == "gemini" and self._probe_gemini():
            return self._builders.get(name)
        return None

    def available_providers(self) -> list[str]:
        return sorted(self._builders)

    def _probe_gemini(self) -> bool:
        if self._gemini_probed:
            return "gemini" in self._builders
        self._gemini_probed = True
        gemini = GeminiBuilder()
        if gemini.available:
            self._builders["gemini"] = gemini
            return True
        return False
