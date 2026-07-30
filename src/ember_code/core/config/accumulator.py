"""``SettingsAccumulator`` — typed pipeline state for the settings
merge stack.

Replaces the pre-refactor ``SettingsLoader._config: dict[str, Any]``
raw-dict field. The accumulator carries the merged config through
the tier pipeline as a typed value object, exposes typed
:meth:`merge` / :meth:`merge_models` operations, and defers final
Pydantic validation to :meth:`to_settings` (which accepts the
:class:`Settings` class as an argument so this module doesn't have
to import it — that would create a cycle with :mod:`settings`).

Why still a dict inside a Pydantic model? Pydantic sub-schemas
enforce their own defaults; the pipeline needs to distinguish "user
config didn't set this" from "user config set this to the default
value" so an aggressive early validation would collapse those
cases. The dict lives inside a value class here so callers get a
typed accessor surface (``merge`` / ``merge_models``) instead of
mutating a bare ``dict[str, Any]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.config.config_io import DictMerger

if TYPE_CHECKING:
    from ember_code.core.config.schemas.models import ModelsConfig


class SettingsAccumulator(BaseModel):
    """Typed carrier for the mid-pipeline config state.

    Wraps the accumulating ``payload`` dict so every mutation goes
    through a typed method (:meth:`merge` / :meth:`merge_models`)
    rather than direct dict indexing. Returns a fresh accumulator
    on each mutation — the underlying dict is copied by
    :meth:`DictMerger.deep`, so the caller can safely branch or
    retain earlier snapshots for diagnostics.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_defaults(cls, defaults_payload: dict[str, Any]) -> SettingsAccumulator:
        """Seed a fresh accumulator from a ``Settings.default_dict()``
        result. Kept as a classmethod (rather than making the
        accumulator import :class:`Settings` itself) so this module
        stays free of the ``settings`` dependency.
        """
        return cls(payload=dict(defaults_payload))

    def merge(self, override: dict[str, Any]) -> SettingsAccumulator:
        """Return a NEW accumulator with ``override`` deep-merged
        into the current payload. No-op (returns ``self``) when
        ``override`` is empty.
        """
        if not override:
            return self
        return SettingsAccumulator(payload=DictMerger.deep(self.payload, override))

    def merge_models(self, models: ModelsConfig) -> SettingsAccumulator:
        """Typed slot for the cloud-migration output. Replaces the
        ``self._config["models"] = migrated.model_dump()`` mid-
        pipeline dict round-trip with a semantically named method
        that dumps once at the boundary.
        """
        new_payload = dict(self.payload)
        new_payload["models"] = models.model_dump()
        return SettingsAccumulator(payload=new_payload)

    def models_block(self) -> dict[str, Any] | None:
        """Return the current ``models`` sub-block as a raw dict, or
        ``None`` when the accumulator carries no ``models`` key
        (bootstrap path). Used by the cloud-migration tier to
        decide whether it has anything to migrate.
        """
        block = self.payload.get("models")
        return block if isinstance(block, dict) else None

    def to_settings(self, settings_cls: type) -> Any:
        """Validate the accumulated payload through the caller-supplied
        :class:`Settings` class. The class is passed IN (rather than
        imported at module top) so this module has no dependency on
        :mod:`settings` and the cycle stays broken.
        """
        return settings_cls(**self.payload)
