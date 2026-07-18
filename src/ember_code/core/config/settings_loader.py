"""``SettingsLoader`` — coordinator for the multi-tier settings merge.

Rewritten around a typed :class:`SettingsAccumulator` and a
data-driven :class:`SettingsMergePlan` — the pre-refactor
hardcoded 7-step imperative body in :meth:`load` is now a call to
``SettingsMergePlan.default(...).run()``. Each precedence tier is a
polymorphic :class:`Tier` subclass; adding a new tier is data (append
a Tier), not editing the middle of a method.

Precedence (highest first — later merges win):

    1. Managed policy (sysadmin-controlled, OS-specific path)
    2. CLI flags
    3. .ember/config.local.yaml (project, gitignored)
    4. .ember/config.yaml (project, committed)
    5. ~/.ember/settings.json (permissions fragment)
    6. ~/.ember/config.yaml (user global)
    7. Built-in defaults (from ``Settings.default_dict()``)

Managed sits ABOVE CLI on purpose — the whole point is that a user
can't override an org policy by adding ``--auto-approve`` on the
command line. Same precedence ordering as Claude Code's managed >
CLI > local > project > user stack.

The tier ORDER is owned by :meth:`SettingsMergePlan.default` — if a
future refactor needs to reorder precedence, that's the one seam.

Collaborators:

* :class:`SettingsAccumulator` — typed pipeline state.
* :class:`SettingsMergePlan` — ordered tier pipeline.
* :class:`ManagedPolicySource` — platform path + YAML/JSON fragment.
* :class:`CloudModelMigrator` — typed migration for cloud rows.
* :class:`DictMerger` / :class:`YamlSource` — low-level I/O
  primitives shared with :class:`ManagedPolicySource`.

Test-seam preservation: the loader retains four thin delegating
shims — :meth:`platform_managed_settings_path`, :meth:`deep_merge`,
:meth:`load_yaml`, :meth:`is_ember_cloud_url` — because eight
monkeypatch sites in the test suite reach the class attribute
directly. Each shim delegates to the real owner (``ManagedPolicySource``,
``DictMerger``, ``YamlSource``, ``ModelRegistryEntry`` respectively)
so the loader itself no longer owns those primitives.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from warnings import warn

from ember_code.core.config.accumulator import SettingsAccumulator
from ember_code.core.config.cloud_model_migrator import CloudModelMigrator, MigrationResult
from ember_code.core.config.config_io import DictMerger, YamlSource
from ember_code.core.config.managed_policy import ManagedPolicySource
from ember_code.core.config.merge_plan import (
    CliTier,
    CloudMigrationTier,
    JsonFragmentTier,
    ManagedTier,
    SettingsMergePlan,
    Tier,
    YamlTier,
)
from ember_code.core.config.model_entry import ModelRegistryEntry

if TYPE_CHECKING:
    from ember_code.core.config.models import CliOverrides
    from ember_code.core.config.settings import Settings


class SettingsLoader:
    """Coordinator for the multi-tier settings merge pipeline.

    The public API is intentionally small: :meth:`load` runs the
    full stack, :meth:`from_config` and the chainable ``merge_*``
    methods are kept as documented seams for callers that want to
    drive one specific tier without going through the full pipeline
    (external plugins that build a partial config, tests that
    exercise one merge step in isolation).

    Internal state is a :class:`SettingsAccumulator` — every mutation
    goes through a typed method rather than direct dict indexing,
    and each ``merge_*`` step returns ``self`` so calls chain.
    """

    def __init__(
        self,
        *,
        accumulator: SettingsAccumulator | None = None,
    ) -> None:
        """Build a fresh loader seeded from ``Settings`` defaults."""
        if accumulator is None:
            # Lazy import: :class:`Settings` imports us (SettingsLoader
            # is re-exported from ``settings.py``). Threading defaults
            # through :meth:`Settings.default_dict` keeps the "defaults
            # are the model's Field defaults" invariant without
            # forcing a top-level cycle.
            from ember_code.core.config.settings import Settings

            accumulator = SettingsAccumulator.from_defaults(Settings.default_dict())
        self._accumulator = accumulator

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> SettingsLoader:
        """Build a loader seeded with a pre-existing config dict —
        the entry point for external callers that already have a
        merged config dict and want to run one specific pipeline
        step (migration, normalisation) without going through the
        full :meth:`load` stack.
        """
        return cls(accumulator=SettingsAccumulator(payload=dict(config)))

    # ── Test seam (delegating shims) ─────────────────────────────────
    #
    # The pre-refactor loader owned four primitives inline:
    # ``platform_managed_settings_path`` (path discovery),
    # ``deep_merge`` + ``load_yaml`` (I/O), and
    # ``is_ember_cloud_url`` (cloud identity). Each has a real home
    # elsewhere now (``ManagedPolicySource`` / ``DictMerger`` /
    # ``YamlSource`` / ``ModelRegistryEntry``), but the test suite
    # monkey-patches the loader's class attributes and reaches them
    # via ``SettingsLoader.<name>``. We keep one-line delegating
    # shims so the tests don't have to rewrite every patch site in
    # the same diff.

    @staticmethod
    def platform_managed_settings_path() -> Path | None:
        """OS-specific path for the sysadmin-enforced managed policy
        file. Delegates to :meth:`ManagedPolicySource.platform_path`.
        """
        return ManagedPolicySource.platform_path()

    @staticmethod
    def deep_merge(base: dict, override: dict) -> dict:
        """Deep merge ``override`` into ``base``. Delegates to
        :meth:`DictMerger.deep` — the real home."""
        return DictMerger.deep(base, override)

    @staticmethod
    def load_yaml(path: Path) -> dict:
        """Load YAML file, returning empty dict on missing/non-dict.
        Delegates to :meth:`YamlSource.load` — the real home."""
        return YamlSource(path).load()

    @classmethod
    def is_ember_cloud_url(cls, url: str) -> bool:
        """True only when the URL points at the production cloud
        host. Delegates to :meth:`ModelRegistryEntry.is_cloud_gateway_url`
        — the real home. Emits a :class:`DeprecationWarning` so
        external callers get one release cycle to migrate.
        """
        warn(
            "SettingsLoader.is_ember_cloud_url is deprecated; use "
            "ModelRegistryEntry.is_cloud_gateway_url instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return ModelRegistryEntry.is_cloud_gateway_url(url)

    # ── Accumulator accessor (legacy compat) ─────────────────────────

    @property
    def config(self) -> dict[str, Any]:
        """Read-back for the accumulated config dict. Kept for
        legacy callers built via :meth:`from_config` that need the
        mutated dict returned to them."""
        return self._accumulator.payload

    # ── merge helpers (chainable pipeline steps) ─────────────────────

    def merge_yaml(self, path: Path) -> SettingsLoader:
        """Merge a YAML file into the accumulator. No-op if the file
        doesn't exist or contains a non-dict payload."""
        self._accumulator = YamlTier(path).apply(self._accumulator)
        return self

    def merge_json_fragment(self, path: Path) -> SettingsLoader:
        """Merge only the whitelisted top-level keys from a CC-style
        ``settings.json`` file. Silently no-ops on missing / malformed
        files."""
        self._accumulator = JsonFragmentTier(path).apply(self._accumulator)
        return self

    def merge_cli(self, overrides: CliOverrides | dict[str, Any] | None) -> SettingsLoader:
        """Merge in-memory CLI overrides. ``None`` / empty is a no-op.

        Accepts either the typed :class:`CliOverrides` bundle OR a
        raw dict for back-compat with call sites (production +
        tests) that still hand-build the dict shape.
        """
        self._accumulator = CliTier(overrides).apply(self._accumulator)
        return self

    def merge_managed(self) -> SettingsLoader:
        """Merge the sysadmin-controlled managed-policy YAML file.
        Runs LAST in the pipeline so it wins over CLI — the "you
        can't ``--auto-approve`` your way out of org policy" tier.

        Routes through :attr:`platform_managed_settings_path` (the
        loader's own delegating shim) so tests that monkey-patch
        the seam on this class keep working.
        """
        path = self.platform_managed_settings_path()
        if path is None:
            return self
        self._accumulator = self._accumulator.merge(YamlSource(path).load())
        return self

    def migrate_cloud_models(self) -> SettingsLoader:
        """Override Ember Cloud models in user config with latest
        defaults.

        Delegates to :class:`CloudModelMigrator` which operates on
        the typed :class:`ModelsConfig` and returns a
        :class:`MigrationResult`. Malformed models blocks are
        skipped (result.ok is False) rather than raising mid-pipeline
        — the final :meth:`finalize` step will surface a clear
        ``ValidationError`` if the payload is beyond repair.
        """
        from ember_code.core.config.settings import Settings

        result: MigrationResult = CloudModelMigrator.migrate_from_dict(
            self._accumulator.models_block(),
            defaults=Settings.defaults().models,
        )
        if result.ok and result.models is not None:
            self._accumulator = self._accumulator.merge_models(result.models)
        return self

    def finalize(self) -> Settings:
        """Validate the accumulated payload through Pydantic and
        return the ``Settings`` instance. Shape errors surface here
        rather than mid-pipeline."""
        from ember_code.core.config.settings import Settings

        return self._accumulator.to_settings(Settings)

    # ── full-pipeline entrypoint ──────────────────────────────────

    @classmethod
    def load(
        cls,
        cli_overrides: CliOverrides | dict[str, Any] | None = None,
        project_dir: Path | None = None,
    ) -> Settings:
        """Run the full multi-tier merge and return a validated
        ``Settings``. See class docstring for precedence.

        Body drives :class:`SettingsMergePlan` — the tier order
        lives in :meth:`SettingsMergePlan.default`. We thread
        :meth:`platform_managed_settings_path` (the loader's own
        delegating shim) into the plan as the managed-path
        provider so tests that monkey-patch that class attribute
        keep controlling the managed tier.
        """
        from ember_code.core.config.settings import Settings

        accumulator = SettingsAccumulator.from_defaults(Settings.default_dict())
        plan = SettingsMergePlan.default(
            project_dir=project_dir,
            cli=cli_overrides,
            accumulator=accumulator,
            settings_cls=Settings,
            defaults_models=Settings.defaults().models,
            managed_path_provider=cls.platform_managed_settings_path,
        )
        return plan.run()


# ── Re-exports for external callers ────────────────────────────────
#
# The pre-refactor module owned ``deep_merge`` / ``load_yaml`` as
# staticmethods on ``SettingsLoader``. Those have moved to
# :mod:`config_io` (real home) but the ``SettingsLoader`` shims
# above still delegate through, so no external caller needs to
# update its import. Callers writing NEW code should reach for
# :class:`DictMerger` and :class:`YamlSource` directly.

__all__ = [
    "CliTier",
    "CloudMigrationTier",
    "JsonFragmentTier",
    "ManagedTier",
    "SettingsAccumulator",
    "SettingsLoader",
    "SettingsMergePlan",
    "Tier",
    "YamlTier",
]
