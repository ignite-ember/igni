"""``CloudModelMigrator`` — replace Ember Cloud rows with latest defaults.

Extracted from :meth:`SettingsLoader.migrate_cloud_models` so the
migration logic operates on a typed :class:`ModelsConfig` rather
than reach-through raw dicts on the loader's accumulator. Any
registry row whose URL points at the production cloud host is
managed by us, so we replace it with the current default to ensure
users always get the latest model after upgrading.

Rule-1 fix: the previous inline implementation did raw-dict
``entry.get("url", "")`` chains. This class uses
:meth:`ModelsConfig.iter_registry_entries` for typed cursor access
and :meth:`ModelRegistryEntry.matches_cloud_gateway` for cloud-URL
detection so the migration path never touches raw-dict shape guards.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ember_code.core.config.model_entry import ModelRegistryEntry
from ember_code.core.config.schemas.models import ModelsConfig

logger = logging.getLogger(__name__)


class MigrationResult(BaseModel):
    """Typed outcome of a cloud-model migration attempt.

    Replaces the previous "try / except Exception / return self"
    swallowing in :meth:`SettingsLoader.migrate_cloud_models` with an
    explicit success / skip contract. The migration tier composes on
    :attr:`ok` — a False result carries a :attr:`reason` so the
    caller can log the skip cause without the mid-pipeline
    ``ValidationError`` propagating up as a session-boot crash.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    models: ModelsConfig | None = None
    reason: str | None = None


class CloudModelMigrator:
    """Migrate cloud-managed registry rows to the shipping defaults.

    Not a Pydantic model — this is a coordinator that carries the
    working :class:`ModelsConfig` state and yields a new one via
    :meth:`migrate`. The instance holds no mutable state beyond the
    two dependencies (``current`` + ``defaults``) so re-runs on the
    same instance are safe.
    """

    def __init__(
        self,
        current: ModelsConfig,
        defaults: ModelsConfig,
    ) -> None:
        # ``defaults`` is required — the caller (the migration tier)
        # already has cheap access to ``Settings.defaults()`` and
        # passes ``.models`` in. Removing the ``None`` default kills
        # the inline ``Settings`` import that used to live here.
        self._current = current
        self._defaults = defaults

    @classmethod
    def migrate_from_dict(
        cls,
        models_block: dict[str, Any] | None,
        defaults: ModelsConfig,
    ) -> MigrationResult:
        """Validate a raw models-block dict, run the migration, and
        return a typed :class:`MigrationResult`.

        Owns the previously inline ``ModelsConfig.model_validate``
        try/except that used to live in
        :meth:`SettingsLoader.migrate_cloud_models` — the narrow
        ``ValidationError`` catch stays here (not up in the loader)
        so the migration boundary owns its own shape guard.
        """
        if not isinstance(models_block, dict):
            return MigrationResult(ok=False, reason="no models block")
        try:
            current = ModelsConfig.model_validate(models_block)
        except ValidationError as exc:
            logger.debug("cloud migration skipped: models block invalid (%s)", exc)
            return MigrationResult(ok=False, reason=f"validation error: {exc}")
        migrated = cls(current, defaults).migrate()
        return MigrationResult(ok=True, models=migrated)

    def migrate(self) -> ModelsConfig:
        """Return a new :class:`ModelsConfig` with cloud rows updated
        to shipping defaults. When the current config has no registry
        entries, returns the input unchanged.
        """
        if not self._current.registry:
            return self._current

        default_cloud = self._build_default_cloud_lookup()
        # Materialise as a plain dict so mutations don't touch the
        # heterogeneous ``dict[str, ModelRegistryEntry | dict]`` shape
        # of the source. We rebuild the returned :class:`ModelsConfig`
        # from this normalised dict.
        new_registry: dict[str, Any] = dict(self._current.registry)

        # Replace user's cloud rows with the shipping defaults.
        for name, entry in list(self._current.iter_registry_entries()):
            if not entry.matches_cloud_gateway():
                continue
            if name in default_cloud:
                # Update existing entry with latest defaults.
                new_registry[name] = dict(default_cloud[name])
                continue
            # Old cloud model no longer in defaults — replace with the
            # current default model, preserving the row's ``model_id``.
            default_name = self._current.default
            if default_name in default_cloud:
                replacement = dict(default_cloud[default_name])
                replacement["model_id"] = default_cloud[default_name]["model_id"]
                new_registry[name] = replacement

        # Ensure the default model exists in the registry.
        default_model = self._current.default
        if default_model and default_model not in new_registry and default_model in default_cloud:
            new_registry[default_model] = dict(default_cloud[default_model])

        return self._current.model_copy(update={"registry": new_registry})

    def _build_default_cloud_lookup(self) -> dict[str, dict[str, Any]]:
        """Return the shipping-defaults registry projected down to
        the cloud rows only, keyed by name and normalised to raw
        dict rows (so the migration output is uniform)."""
        lookup: dict[str, dict[str, Any]] = {}
        for name, raw in self._defaults.registry.items():
            entry = (
                raw
                if isinstance(raw, ModelRegistryEntry)
                else ModelRegistryEntry.model_validate(raw)
            )
            if entry.matches_cloud_gateway():
                lookup[name] = entry.model_dump()
        return lookup
