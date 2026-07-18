"""``ModelsConfig`` — the ``models`` block of ``Settings``.

Extracted from :mod:`ember_code.core.config.settings` so a single-domain
config schema lives in a single-domain file. Registry-shape query
methods (``current_uses_cloud_token`` / ``find_non_cloud_fallback`` /
``iter_registry_entries``) stay on the class so downstream readers
never have to coerce raw dicts on their own.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, Field

from ember_code.core.config.model_entry import CLOUD_TOKEN_SENTINEL, ModelRegistryEntry


class ModelsConfig(BaseModel):
    # Empty means "auto" — the resolver falls back to the first key
    # in ``registry`` at lookup time. Cloud discovery sets this at
    # session start once the cloud catalogue is merged in. Users
    # explicitly pinning a model via ``/model`` or config override
    # set it directly.
    default: str = ""
    max_context_window: int = 200_000
    max_run_timeout: int = 300  # total timeout for a single arun() call (seconds)
    # Retry count for transient model-API failures (timeouts, 5xx). Applied
    # to both the main team agent and pool specialists. Surfaced here so
    # users can tune it from settings without touching code.
    retries: int = 2
    # Registry is heterogeneous by design: cloud discovery writes
    # typed :class:`ModelRegistryEntry` instances (via
    # :meth:`CloudModelCatalogClient.merge_into`), while user YAML
    # entries load as raw dicts. :meth:`ModelRegistry._coerce_entry`
    # normalises both at lookup time.
    registry: dict[str, ModelRegistryEntry | dict[str, Any]] = Field(default_factory=dict)

    # ── Registry-shape queries (heterogeneous coercion lives here) ──
    #
    # ``registry`` intentionally holds a mix of typed
    # :class:`ModelRegistryEntry` instances and raw dicts (see the
    # field comment above). Rather than every caller re-implement
    # the "is this a cloud entry? give me a fallback name" logic
    # against the shape variance, the accessors below own it.
    # :class:`AuthCommand` (``/logout``) is the primary caller; any
    # future model-picker path that needs to reason about cloud vs
    # inline api_keys should reuse these instead of coercing on its
    # own.

    def _entry(self, raw: ModelRegistryEntry | dict[str, Any] | None) -> ModelRegistryEntry | None:
        """Normalise a registry row to a typed
        :class:`ModelRegistryEntry`. ``None`` passes through so
        callers can chain ``.get(name)`` results directly."""
        if raw is None:
            return None
        if isinstance(raw, ModelRegistryEntry):
            return raw
        return ModelRegistryEntry.model_validate(raw)

    def iter_registry_entries(self) -> Iterator[tuple[str, ModelRegistryEntry]]:
        """Yield ``(name, entry)`` tuples with every registry row
        normalised to :class:`ModelRegistryEntry`.

        Consumers of ``registry`` that need typed access (the
        :class:`CloudModelMigrator` in particular) previously had to
        write raw-dict ``.get('url', '')`` chains against
        heterogeneous rows. This cursor centralises the coercion so
        the migrator can rely on ``entry.matches_cloud_gateway()``
        instead of re-implementing URL-parse logic per caller.
        """
        for name, raw in self.registry.items():
            entry = self._entry(raw)
            if entry is None:
                continue
            yield name, entry

    def current_uses_cloud_token(self) -> bool:
        """True when :attr:`default` resolves to a registry entry
        whose ``api_key`` is the cloud sentinel. False when the
        default is unset, missing from the registry, or configured
        with an inline / env-backed api_key."""
        entry = self._entry(self.registry.get(self.default))
        if entry is None:
            return False
        return entry.api_key == CLOUD_TOKEN_SENTINEL

    def find_non_cloud_fallback(self) -> str | None:
        """Return the first registry name whose entry has a non-cloud
        ``api_key`` (any non-empty string that isn't
        :data:`CLOUD_TOKEN_SENTINEL`), or ``None`` when the registry
        has no such entry.

        Used by ``/logout`` to keep the session usable after cloud
        credentials are dropped — we don't want the default model
        to remain cloud-backed with no token behind it."""
        for name, raw in self.registry.items():
            entry = self._entry(raw)
            if entry is None:
                continue
            key = entry.api_key
            if key and key != CLOUD_TOKEN_SENTINEL:
                return name
        return None
