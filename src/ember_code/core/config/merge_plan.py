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

Two kinds of block step outside that order: ``workspace_trust`` and
``telemetry`` come only from the organisation (``OrganisationOnlyTier``),
and ``hooks.group`` only from the developer (``DeveloperOnlyTier``).

Managed sits ABOVE CLI on purpose — the whole point is that a user
can't override an org policy by adding ``--auto-approve`` on the
command line.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ember_code.core.config.accumulator import SettingsAccumulator
from ember_code.core.config.cloud_model_migrator import CloudModelMigrator
from ember_code.core.config.config_io import YamlSource
from ember_code.core.config.managed_policy import ManagedPolicySource
from ember_code.core.config.schemas.telemetry import OtlpConfig, TelemetryConfig
from ember_code.core.layer import Layers
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


class ProjectYamlTier(YamlTier):
    """A project's YAML file, less the keys a repository may not set.

    A project's ``.igni/config.yaml`` arrives with the repository: anyone
    who can commit to it writes it. ``network`` says which hosts an
    air-gapped igni may reach, so a project that could set it could open
    the air gap for whoever clones it. It comes from the user's file, the
    group policy or the managed file, and a project's is dropped with a
    warning rather than merged.
    """

    #: Top-level keys a project file may not set.
    #: Keys a repository may not set. A repository's config is whoever
    #: wrote the repository, so it may neither switch air-gapped mode off,
    #: name a host igni would then treat as inside the network, nor move
    #: ``storage.data_dir`` — the organisation's group policy is read from
    #: under it, so moving it would hide the policy or plant a forged one.
    #: A dotted key is one field inside a block.
    _NOT_FROM_A_PROJECT: tuple[str, ...] = (
        "storage.data_dir",
        "network",
        "air_gapped",
        "api_url",
        "portal_url",
        "update_endpoint",
        "plugins.marketplaces",
    )

    #: Further keys a project may set only once the folder is trusted
    #: (``core/workspace_trust``): its permission rules and mode, and the
    #: safety lists and Bypass guard. An untrusted repository could otherwise
    #: empty ``safety.blocked_commands``, ``allow: [Bash]`` or start in
    #: ``bypassPermissions`` for whoever opens it.
    _ONLY_WHEN_TRUSTED: tuple[str, ...] = ("permissions", "safety")

    def __init__(self, path: Path, *, trusted: Callable[[], bool] | None = None) -> None:
        super().__init__(path)
        #: Asked at :meth:`apply` time; ``None`` means trusted (direct
        #: construction in tests and tooling, which predates trust).
        self._trusted = trusted

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        payload = drop_project_keys(
            dict(self._source.load()),
            self._NOT_FROM_A_PROJECT,
            source=self._source.path,
            why="only user, group or managed settings may set",
        )
        if self._trusted is not None and payload and not self._trusted():
            payload = drop_project_keys(
                payload,
                self._ONLY_WHEN_TRUSTED,
                source=self._source.path,
                why="a project may set only once you trust the folder (igni trust)",
            )
        return accumulator.merge(payload)


def drop_project_keys(
    payload: dict[str, Any], keys: tuple[str, ...], *, source: Path, why: str
) -> dict[str, Any]:
    """``payload`` less ``keys`` (a dotted key is one field in a block), with a warning."""
    refused: list[str] = []
    for key in keys:
        block, _, field = key.partition(".")
        if not field:
            if key in payload:
                refused.append(key)
                payload = {k: v for k, v in payload.items() if k != key}
        elif isinstance(payload.get(block), dict) and field in payload[block]:
            refused.append(key)
            payload = {**payload, block: {k: v for k, v in payload[block].items() if k != field}}
    if refused:
        logger.warning("%s sets %s, which %s; ignored.", source, ", ".join(refused), why)
    return payload


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


class ProjectJsonFragmentTier(JsonFragmentTier):
    """A project's ``settings.json`` / ``settings.local.json``.

    The lifted keys are ``permissions`` only, so an untrusted folder's file
    contributes nothing: see :attr:`ProjectYamlTier._ONLY_WHEN_TRUSTED`.
    """

    def __init__(self, path: Path, *, trusted: Callable[[], bool] | None = None) -> None:
        super().__init__(path)
        self._trusted = trusted

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        result = ManagedPolicySource.load_json_fragment(self._path)
        if not (result.ok and result.data):
            return accumulator
        payload = dict(result.data)
        if self._trusted is not None and not self._trusted():
            payload = drop_project_keys(
                payload,
                ProjectYamlTier._ONLY_WHEN_TRUSTED,
                source=self._path,
                why="a project may set only once you trust the folder (igni trust)",
            )
        return accumulator.merge(payload) if payload else accumulator


class OrganisationOnlyTier(Tier):
    """A settings block that comes from the organisation, never from below it.

    Runs after the CLI tier and before the group and managed tiers: whatever
    the user's config, a project's or the command line said is put back to the
    defaults, and only the group policy pack, the managed file and the
    environment (applied by the block's own model) can set it. Subclasses name
    the block and how to build its defaults.
    """

    #: The top-level settings key.
    BLOCK: ClassVar[str]

    def defaults(self) -> dict[str, Any]:
        """The block's defaults, without its environment."""
        raise NotImplementedError

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        defaults = self.defaults()
        payload = dict(accumulator.payload)
        block = payload.get(self.BLOCK)
        if isinstance(block, dict) and any(
            key in defaults and block[key] != defaults[key] for key in block
        ):
            logger.warning(
                "%s may be set only in the managed-settings file, the group policy or "
                "the environment; the value from user, project or CLI settings is ignored.",
                self.BLOCK,
            )
        payload[self.BLOCK] = defaults
        return SettingsAccumulator(payload=payload)


class WorkspaceTrustTier(OrganisationOnlyTier):
    """``workspace_trust`` comes from the organisation, never from below it.

    The environment may only tighten it (``WorkspaceTrustConfig``). A
    repository that could set this could trust itself; a person who could set
    it could opt out of their organisation's rule.
    """

    BLOCK = "workspace_trust"

    def defaults(self) -> dict[str, Any]:
        from ember_code.core.config.schemas.workspace_trust import WorkspaceTrustConfig

        return WorkspaceTrustConfig.model_construct().model_dump()


class TelemetryTier(OrganisationOnlyTier):
    """``telemetry`` (the OTLP collector) comes from the organisation.

    A repository that could set it could send whoever opens it's traces to a
    host of the repository's choosing, and with ``capture_content`` their
    prompts and code; a person could do the same to their organisation. The
    ``OTEL_*`` environment fills what the organisation left unset
    (``OtlpConfig``).
    """

    BLOCK = "telemetry"

    def defaults(self) -> dict[str, Any]:
        return TelemetryConfig.model_construct(otlp=OtlpConfig.model_construct()).model_dump()


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

    #: ``hooks.group`` as the command line set it (``--strict`` sets
    #: ``ask``), or ``None``. Read by :class:`DeveloperOnlyTier`.
    hooks_group: str | None = None

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
        hooks = payload.get("hooks")
        self.hooks_group = hooks.get("group") if isinstance(hooks, dict) else None
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


class ManagedOnlyTier(Tier):
    """Settings only the managed-settings file (or the environment) may set.

    ``group_policy.trusted_keys`` and ``group_policy.required`` decide whether
    the organisation's group policy is enforced at all. Whoever could set
    them could pin a key of their own, or make an unusable pack harmless —
    so a value from the user's config, a project's, the CLI or the group
    pack itself is discarded here, and only the managed file's survives.
    The environment (``IGNI_GROUP_POLICY_TRUSTED_KEYS``,
    ``IGNI_GROUP_POLICY_REQUIRED``) is applied afterwards by
    ``GroupPolicyConfig`` — it is a deployment's decision, made where the
    process is started.

    Runs after :class:`ManagedTier`, reading the same file through the same
    path provider.
    """

    #: ``group_policy`` fields only the managed file may set.
    FIELDS: tuple[str, ...] = ("trusted_keys", "required")

    def __init__(
        self,
        path_provider: Callable[[], Path | None] = ManagedPolicySource.platform_path,
    ) -> None:
        self._path_provider = path_provider

    @classmethod
    def managed_values(cls, path_provider: Callable[[], Path | None]) -> dict[str, Any]:
        """What the managed file sets of :attr:`FIELDS`, and nothing else."""
        path = path_provider()
        payload = YamlSource(path).load() if path is not None else {}
        block = payload.get("group_policy") if isinstance(payload, dict) else None
        if not isinstance(block, dict):
            return {}
        return {field: block[field] for field in cls.FIELDS if field in block}

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        from ember_code.core.config.schemas.storage_and_paths import GroupPolicyConfig

        managed = self.managed_values(self._path_provider)
        defaults = GroupPolicyConfig.model_construct().model_dump()
        payload = dict(accumulator.payload)
        block = dict(payload.get("group_policy") or {})
        refused = [
            field
            for field in self.FIELDS
            if field not in managed and field in block and block[field] != defaults[field]
        ]
        if refused:
            logger.warning(
                "group_policy.%s may be set only in the managed-settings file or the "
                "environment; the value from user, project, CLI or group settings is ignored.",
                ", group_policy.".join(refused),
            )
        for field in self.FIELDS:
            block[field] = managed.get(field, defaults[field])
        payload["group_policy"] = block
        return SettingsAccumulator(payload=payload)


class DeveloperOnlyTier(Tier):
    """Settings only the developer may set: their own config and their command line.

    The mirror of :class:`OrganisationOnlyTier`. ``hooks.group`` decides
    whether the hooks an organisation's group policy ships run on this
    machine. Those hooks run ``bash -c`` outside the agent's permissions, and
    the person a developer needs protecting from there is whoever administers
    the organisation — who writes the group policy, and may well write the
    managed file too. So the group pack, the managed file and a project's
    config (whoever wrote the repository) cannot set it: a value from any of
    them is discarded, with a warning, and what stands is the stricter of
    ``~/.igni/config.yaml`` and the command line (``--strict`` means
    ``ask``).

    Runs after every tier that could have set it.
    """

    #: ``(block, field)`` for each developer-only setting.
    FIELDS: tuple[tuple[str, str], ...] = (("hooks", "group"),)

    def __init__(self, user_config: Path | None, cli_tier: CliTier | None = None) -> None:
        #: ``~/.igni/config.yaml``, or ``None`` where this layer reads no
        #: local settings (the cloud's).
        self._user_config = user_config
        self._cli_tier = cli_tier

    def _user_value(self) -> str | None:
        from ember_code.core.config.schemas.orchestration import GROUP_HOOK_MODES

        if self._user_config is None:
            return None
        block = YamlSource(self._user_config).load().get("hooks")
        value = block.get("group") if isinstance(block, dict) else None
        if value is not None and value not in GROUP_HOOK_MODES:
            logger.warning(
                "%s sets hooks.group to %r; it must be one of %s. Using 'ask'.",
                self._user_config,
                value,
                ", ".join(GROUP_HOOK_MODES),
            )
            # A typo in a safety setting is read as the cautious middle,
            # not as the permissive default.
            return "ask"
        return value

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        from ember_code.core.config.schemas.orchestration import GROUP_HOOK_MODES

        chosen = [
            v
            for v in (self._user_value(), self._cli_tier.hooks_group if self._cli_tier else None)
            if v in GROUP_HOOK_MODES
        ]
        value = max(chosen, key=GROUP_HOOK_MODES.index, default="run")

        payload = dict(accumulator.payload)
        block = dict(payload.get("hooks") or {})
        if "group" in block and block["group"] != value:
            logger.warning(
                "hooks.group may be set only in your own ~/.igni/config.yaml or on the "
                "command line; %r from a project, the group policy or the managed file "
                "is ignored, and %r stands.",
                block["group"],
                value,
            )
        block["group"] = value
        payload["hooks"] = block
        return SettingsAccumulator(payload=payload)


class CloudMigrationTier(Tier):
    """Override igni server rows with the shipping defaults so users
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

    ## Where the fetcher comes from

    :meth:`SettingsMergePlan.default` only appends the tier when it is handed
    a ``group_policy_fetcher``, and for a long time no production caller
    passed one — the tier was written, correct, and unreached, so an admin
    publishing ``{"permissions": {"web_fetch": "deny"}}`` believed it was enforced
    and it was not.

    Production now passes
    :class:`~ember_code.core.config.group_policy.CachedSettingsFetcher`, from
    the two places that build a real ``Settings``: ``BackendApp.run`` (the
    backend process every window talks to) and ``load_settings_from_options``
    (the ``igni -m`` / pipe path, which runs a session in-process). Both build
    exactly one per process.

    That fetcher reads the pack the last refresh left on disk rather than
    calling the portal, because a settings load runs before this process holds
    a bearer token. The fetch lives in ``AuthController._hydrate_group_policy``
    — cold start, login, and every ``group_policy.poll_seconds`` — and the read
    lives here; see :class:`CachedSettingsFetcher` for what that split buys and
    what it costs.

    ## Only a pack igni server signed

    The cache is a directory the user can write, so what the fetcher reads is
    trusted only as far as igni server's signature on it: the pack is stored
    exactly as signed (``pack.jws``) and verified — signature, ``iss`` against
    ``api_url``, ``sub`` against the person signed in, no rollback — before it
    reaches this tier. An unusable pack is never merged; with the managed-only
    ``group_policy.required`` the fetcher raises instead. See
    ``core/config/group_policy_signature.py`` and :class:`ManagedOnlyTier`.

    Tests and one-off migrations still pass their own fetcher, and a plan built
    without one skips the tier entirely.
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
        project_trusted: Callable[[], bool] | None = None,
    ) -> SettingsMergePlan:
        """Build the standard 7-tier plan from filesystem paths.

        Ordering here IS the precedence contract — the tier list
        below is the ONE place the merge order is specified, and
        both the loader docstring and the test-suite precedence
        assertions reference this method's output. If a future
        refactor needs to reorder tiers, this is the seam.

        ``project_trusted`` says whether the folder is trusted
        (``core/workspace_trust``); an untrusted folder's config may not set
        ``permissions`` or ``safety``. Left ``None``, the process's
        :class:`~ember_code.core.workspace_trust.WorkspaceTrust` is asked.
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
        if project_trusted is None:
            folder = project_dir

            def project_trusted() -> bool:
                from ember_code.core.workspace_trust import WorkspaceTrust

                return WorkspaceTrust.shared().is_trusted(
                    folder, what="permission and safety settings"
                )

        # Built ahead of the list: ``GroupPolicyTier`` needs the same
        # instance, because the ratchet reads what this one actually set.
        cli_tier = CliTier(cli)

        # A layer may read no settings file of its own — not the user's
        # under ``~/.igni``, not the workspace's ``.igni`` (the cloud's:
        # what it runs with comes from the image and from igni server).
        local_tiers: list[Tier] = (
            []
            if not Layers.current().reads_local_settings()
            else [
                # User global (lowest priority above built-in defaults)
                YamlTier(user_ember / "config.yaml"),
                JsonFragmentTier(user_ember / "settings.json"),
                JsonFragmentTier(user_ember / "settings.local.json"),
                # Project (committed). An untrusted folder's ``permissions``
                # and ``safety`` are dropped (``core/workspace_trust``).
                ProjectYamlTier(project_ember / "config.yaml", trusted=project_trusted),
                ProjectJsonFragmentTier(project_ember / "settings.json", trusted=project_trusted),
                # Project local (gitignored)
                ProjectYamlTier(project_ember / "config.local.yaml", trusted=project_trusted),
                ProjectJsonFragmentTier(
                    project_ember / "settings.local.json", trusted=project_trusted
                ),
            ]
        )
        tiers: list[Tier] = [
            *local_tiers,
            # CLI
            cli_tier,
            # ``workspace_trust`` back to its defaults: only the organisation
            # tiers below (and the environment) may set it.
            WorkspaceTrustTier(),
            # ``telemetry`` likewise: where traces go is the organisation's call.
            TelemetryTier(),
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
            # The settings that decide whether group policy is enforced at
            # all: from the managed file (or the environment), or not at all.
            ManagedOnlyTier(path_provider=managed_path_provider),
            # And the settings only the developer may set — ``hooks.group``,
            # whether the group's hooks run here: from their own config and
            # command line, whatever the tiers above said.
            DeveloperOnlyTier(
                user_ember / "config.yaml" if local_tiers else None,
                cli_tier=cli_tier,
            ),
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
