"""``SettingsMergePlan`` — the ordered tier pipeline that used to
live as a hardcoded 7-step imperative body in
:meth:`SettingsLoader.load`.

Each precedence tier is a :class:`Tier` subclass with an
:meth:`apply` method that folds itself into a
:class:`SettingsAccumulator`. The plan carries an ordered list of
tiers and its :meth:`run` walks them in sequence. Adding a new tier
is now data (append a Tier instance to the list) — not editing the
middle of a method.

Precedence (highest first — later tiers in the list win because
their ``apply`` runs last):

    1. Managed policy (sysadmin-controlled, OS-specific path)
    2. CLI flags
    3. .ember/config.local.yaml + settings.local.json (project)
    4. .ember/config.yaml + settings.json (project)
    5. ~/.ember/settings.local.json (permissions fragment)
    6. ~/.ember/settings.json (permissions fragment)
    7. ~/.ember/config.yaml (user global)
    8. Built-in defaults (from ``Settings.default_dict()`` — seeded
       into the accumulator BEFORE the plan runs)

Managed sits ABOVE CLI on purpose — the whole point is that a user
can't override an org policy by adding ``--auto-approve`` on the
command line.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.core.config.accumulator import SettingsAccumulator
from ember_code.core.config.cloud_model_migrator import CloudModelMigrator
from ember_code.core.config.config_io import YamlSource
from ember_code.core.config.managed_policy import ManagedPolicySource

if TYPE_CHECKING:
    from ember_code.core.config.models import CliOverrides
    from ember_code.core.config.schemas.models import ModelsConfig


class Tier:
    """Base polymorphic tier — one precedence layer in the merge stack.

    Subclasses override :meth:`apply` with the tier-specific merge
    logic. The default no-op implementation exists so a plan with
    a "skipped" tier just returns the accumulator unchanged.
    """

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        return accumulator


class YamlTier(Tier):
    """A YAML file merged into the accumulator. No-op when the file
    doesn't exist or contains a non-dict payload."""

    def __init__(self, path: Path) -> None:
        self._source = YamlSource(path)

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        return accumulator.merge(self._source.load())


class JsonFragmentTier(Tier):
    """A CC-style ``settings.json`` file — only whitelisted top-level
    keys are lifted into the accumulator. Silently no-ops on missing
    / malformed files."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        result = ManagedPolicySource.load_json_fragment(self._path)
        if result.ok and result.data:
            return accumulator.merge(result.data)
        return accumulator


class CliTier(Tier):
    """In-memory CLI overrides merged into the accumulator.

    Accepts either a typed :class:`CliOverrides` bundle (the AP5
    fix — CLI seam becomes typed) OR a raw dict for back-compat with
    call sites (production and tests) that still hand-build the
    dict shape. ``None`` / empty is a no-op.
    """

    def __init__(self, overrides: CliOverrides | dict[str, Any] | None) -> None:
        self._overrides = overrides

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        if self._overrides is None:
            return accumulator
        # Import here to keep :mod:`cli.options` off the package-
        # load path (Agno pulls in on that import).
        from ember_code.core.config.models import CliOverrides

        if isinstance(self._overrides, CliOverrides):
            payload = self._overrides.as_merge_dict()
        else:
            payload = self._overrides
        return accumulator.merge(payload)


class ManagedTier(Tier):
    """Sysadmin-controlled managed-policy YAML file. Runs LAST in
    the pipeline so it wins over CLI — the "you can't
    ``--auto-approve`` your way out of org policy" tier.

    Takes an INJECTED path-provider callable rather than reaching
    directly for :meth:`ManagedPolicySource.platform_path`. The
    :class:`SettingsLoader` passes its own
    :meth:`platform_managed_settings_path` shim in so test
    monkeypatches on ``SettingsLoader.platform_managed_settings_path``
    still take effect (eight patch sites in
    ``tests/test_settings.py`` reach the class attribute directly).
    Default provider is :meth:`ManagedPolicySource.platform_path` so
    callers that don't care about the test-seam route (external
    plugins, direct plan construction) get the natural behaviour.
    """

    def __init__(
        self,
        path_provider: Callable[[], Path | None] = ManagedPolicySource.platform_path,
    ) -> None:
        self._path_provider = path_provider

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        path = self._path_provider()
        if path is None:
            return accumulator
        return accumulator.merge(YamlSource(path).load())


class CloudMigrationTier(Tier):
    """Override Ember Cloud rows with the shipping defaults so users
    always get the latest model after upgrading. Runs after the
    other tiers so a fresh migration is visible in the final
    :class:`Settings`.
    """

    def __init__(self, defaults: ModelsConfig) -> None:
        self._defaults = defaults

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        result = CloudModelMigrator.migrate_from_dict(
            accumulator.models_block(),
            defaults=self._defaults,
        )
        if not result.ok or result.models is None:
            # Malformed models block — leave the accumulator alone
            # so ``finalize`` surfaces the ValidationError on the
            # user's terms rather than mid-pipeline.
            return accumulator
        return accumulator.merge_models(result.models)


class SettingsMergePlan:
    """Ordered pipeline of :class:`Tier` instances.

    :meth:`run` walks the tiers in order, threading the
    :class:`SettingsAccumulator` through each :meth:`Tier.apply`.
    Use :meth:`default` to build the standard 7-tier stack from
    filesystem paths, or construct directly with :meth:`custom` for
    tests and one-off migrations.
    """

    def __init__(
        self,
        tiers: list[Tier],
        *,
        accumulator: SettingsAccumulator,
        settings_cls: type,
    ) -> None:
        self._tiers = tiers
        self._accumulator = accumulator
        self._settings_cls = settings_cls

    @classmethod
    def default(
        cls,
        *,
        project_dir: Path | None,
        cli: CliOverrides | dict[str, Any] | None,
        accumulator: SettingsAccumulator,
        settings_cls: type,
        defaults_models: ModelsConfig,
        managed_path_provider: Callable[[], Path | None] = ManagedPolicySource.platform_path,
    ) -> SettingsMergePlan:
        """Build the standard 7-tier plan from filesystem paths.

        Ordering here IS the precedence contract — the tier list
        below is the ONE place the merge order is specified, and
        both the loader docstring and the test-suite precedence
        assertions reference this method's output. If a future
        refactor needs to reorder tiers, this is the seam.
        """
        user_ember = Path.home() / ".ember"
        if project_dir is None:
            project_dir = Path.cwd()
        project_ember = project_dir / ".ember"

        tiers: list[Tier] = [
            # User global (lowest priority above built-in defaults)
            YamlTier(user_ember / "config.yaml"),
            JsonFragmentTier(user_ember / "settings.json"),
            JsonFragmentTier(user_ember / "settings.local.json"),
            # Project (committed)
            YamlTier(project_ember / "config.yaml"),
            JsonFragmentTier(project_ember / "settings.json"),
            # Project local (gitignored)
            YamlTier(project_ember / "config.local.yaml"),
            JsonFragmentTier(project_ember / "settings.local.json"),
            # CLI
            CliTier(cli),
            # Managed policy — last, so it wins over CLI.
            ManagedTier(path_provider=managed_path_provider),
            # Migration — replaces cloud rows with shipping defaults.
            CloudMigrationTier(defaults=defaults_models),
        ]
        return cls(
            tiers,
            accumulator=accumulator,
            settings_cls=settings_cls,
        )

    def run(self) -> Any:
        """Execute the tier pipeline and return the validated
        :class:`Settings`. Shape errors surface here (via the final
        ``Settings(**payload)`` validation) rather than mid-pipeline.
        """
        accumulator = self._accumulator
        for tier in self._tiers:
            accumulator = tier.apply(accumulator)
        return accumulator.to_settings(self._settings_cls)
