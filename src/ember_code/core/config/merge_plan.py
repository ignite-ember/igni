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
    2. Org-group policy (from the server) — with one exception: it
       cannot loosen a ``permissions`` field the CLI set to ``deny``.
       See ``GroupPolicyTier``.
    3. CLI flags
    4. .igni/config.local.yaml + settings.local.json (project)
    5. .igni/config.yaml + settings.json (project)
    6. ~/.igni/settings.local.json (permissions fragment)
    7. ~/.igni/settings.json (permissions fragment)
    8. ~/.igni/config.yaml (user global)
    9. Built-in defaults (from ``Settings.default_dict()`` — seeded
       into the accumulator BEFORE the plan runs)

Managed sits ABOVE CLI on purpose — the whole point is that a user
can't override an org policy by adding ``--auto-approve`` on the
command line.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.core.config.accumulator import SettingsAccumulator
from ember_code.core.config.cloud_model_migrator import CloudModelMigrator
from ember_code.core.config.config_io import YamlSource
from ember_code.core.config.managed_policy import ManagedPolicySource
from ember_code.core.paths import home_config_dir, project_config_dir

if TYPE_CHECKING:
    from ember_code.core.config.group_policy import GroupPolicyPack
    from ember_code.core.config.models import CliOverrides
    from ember_code.core.config.schemas.models import ModelsConfig


logger = logging.getLogger(__name__)


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

    #: Permission fields this tier set to ``deny``, in the order applied.
    #:
    #: Read by :class:`GroupPolicyTier` so a group can tighten a
    #: permission but never loosen one the operator denied on the command
    #: line. Kept here rather than inferred from the payload because
    #: "the CLI denied this" and "something upstream happened to deny
    #: this" are different facts, and only the first is a ratchet.
    denied_by_cli: frozenset[str] = frozenset()

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

        permissions = payload.get("permissions") or {}
        self.denied_by_cli = frozenset(
            field for field, value in permissions.items() if value == "deny"
        )
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


class GroupPolicyTier(Tier):
    """Org-group policy overrides — fetched from ember-server, cached locally.

    Sits ABOVE CliTier (group-defined settings beat user CLI flags) and
    BELOW ManagedTier (sysadmin file still wins over any org policy).

    ``fetcher`` is a zero-argument callable that returns a
    :class:`~ember_code.core.config.group_policy.GroupPolicyPack` or
    ``None`` when the user has no group. It is invoked once per
    :meth:`Tier.apply` call; callers should memoize or cache at the
    fetcher level to avoid repeated network calls.

    ## The deny ratchet

    Sitting above the CLI is deliberate and one-directional in intent:
    the point is that a user cannot escape org policy by adding
    ``--auto-approve``. The implementation was bidirectional, which is
    not the same thing — a group could turn a developer's ``--strict``
    into ``shell_execute: allow`` and nothing said so. A safety flag that
    silently does nothing is worse than no flag.

    So a permission the CLI set to ``deny`` stays denied. A group can
    still *tighten* anything, and ``--auto-approve`` still cannot get
    past a group's ``deny`` — both directions of the original intent
    survive, and only the loosening of an explicit local deny does not.

    Everything else a group sets still wins over the CLI. This is a
    ratchet on ``permissions`` denies specifically, not a general
    reversal of precedence.
    """

    def __init__(
        self,
        fetcher: Callable[[], GroupPolicyPack | None],  # type: ignore[name-defined]
        cli_tier: CliTier | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._cli_tier = cli_tier
        #: Fields the group tried to loosen and could not, as
        #: ``{field: attempted_value}``. Read by the session to tell the
        #: developer, because a policy that quietly does not apply is its
        #: own kind of surprise.
        self.refused_loosening: dict[str, str] = {}

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        pack = self._fetcher()
        if pack is None:
            return accumulator

        payload = pack.to_settings_dict()
        self.refused_loosening = {}

        denied = self._cli_tier.denied_by_cli if self._cli_tier else frozenset()
        if denied:
            permissions = dict(payload.get("permissions") or {})
            for field in sorted(denied):
                attempted = permissions.get(field)
                if attempted is not None and attempted != "deny":
                    self.refused_loosening[field] = str(attempted)
                    # Drop the key rather than writing 'deny' back: the
                    # accumulator already holds the CLI's deny, and
                    # rewriting it would hide which tier decided.
                    permissions.pop(field)
            if self.refused_loosening:
                payload = {**payload, "permissions": permissions}
                # Said out loud, because a policy that quietly does not
                # apply is its own surprise — the admin believes they set
                # something, and nothing anywhere disagrees.
                logger.warning(
                    "Group policy tried to relax %s, which you denied on the command "
                    "line; the local deny stands. An organisation policy can tighten a "
                    "permission but not loosen one you denied yourself.",
                    ", ".join(
                        f"{field} to {value!r}"
                        for field, value in sorted(self.refused_loosening.items())
                    ),
                )

        return accumulator.merge(payload)


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
        group_policy_fetcher: Callable[[], GroupPolicyPack | None] | None = None,  # type: ignore[name-defined]
    ) -> SettingsMergePlan:
        """Build the standard 7-tier plan from filesystem paths.

        Ordering here IS the precedence contract — the tier list
        below is the ONE place the merge order is specified, and
        both the loader docstring and the test-suite precedence
        assertions reference this method's output. If a future
        refactor needs to reorder tiers, this is the seam.
        """
        # Through the helpers, not ``/ CONFIG_DIR`` — they fall back to
        # the pre-rename directory while that is the one on disk. Built
        # by hand here, this tier list was the one reader that could not
        # see an unmigrated install, and it fails silently: no config
        # found is indistinguishable from no config set, so a session
        # would come up on the built-in defaults without saying so.
        user_ember = home_config_dir()
        if project_dir is None:
            project_dir = Path.cwd()
        project_ember = project_config_dir(project_dir)

        # Built ahead of the list: ``GroupPolicyTier`` needs the same
        # instance, because the ratchet reads what this one actually set.
        cli_tier = CliTier(cli)

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
            cli_tier,
            # Org-group policy — wins over CLI, loses to sysadmin
            # ManagedPolicy. Handed the CLI tier so it can honour the deny
            # ratchet: a group tightens freely but cannot loosen a
            # permission the operator denied on the command line. See
            # ``GroupPolicyTier``.
            *(
                [GroupPolicyTier(fetcher=group_policy_fetcher, cli_tier=cli_tier)]
                if group_policy_fetcher
                else []
            ),
            # Managed policy — last, so it wins over CLI and group policy.
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
