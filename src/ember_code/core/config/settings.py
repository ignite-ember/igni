"""Settings management — composition module.

Only three things live in this module:

* :class:`Settings` — the top-level Pydantic model that aggregates
  every sub-config schema. The model's ``Field`` defaults ARE the
  defaults (call :meth:`Settings.defaults` for a typed instance or
  :meth:`Settings.default_dict` for the dict form the loader seeds
  itself with).
* Re-exports of the sub-config schemas from
  :mod:`ember_code.core.config.schemas` so the flat import surface
  (``from ember_code.core.config.settings import PermissionsConfig``)
  keeps working for downstream callers.
* :func:`load_settings` — the ONE surviving module-level function.
  It's a two-line facade over :meth:`SettingsLoader.load` preserved
  because :mod:`cli.invocation` monkey-patches
  ``_settings_module.load_settings`` for test isolation and that
  seam is load-bearing. Every OTHER former module-level shim
  (``_deep_merge`` / ``_load_yaml`` / ``_platform_managed_settings_path``
  / ``save_default_model`` / …) has been promoted to a method on a
  class — see :class:`SettingsLoader`, :class:`ManagedPolicySource`,
  :class:`UserConfigStore`, and :class:`CloudModelMigrator` in their
  own sibling modules.

The multi-tier merge pipeline (managed > CLI > project.local >
project > user > built-in defaults) is owned by
:class:`SettingsMergePlan` in
:mod:`ember_code.core.config.merge_plan` and driven by
:class:`SettingsLoader` in
:mod:`ember_code.core.config.settings_loader`. Each precedence tier
is a polymorphic :class:`Tier` subclass — adding a tier is a data
change in the plan's default factory, not an edit inside a method
body.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ember_code.core.config.models import CliOverrides

from ember_code.core.config.model_entry import CLOUD_TOKEN_SENTINEL, ModelRegistryEntry

# ── Re-exports: schemas from the schemas package ───────────────────
#
# The pre-refactor module defined every sub-config class inline.
# They now live in ``schemas/`` (one file per domain cluster) and
# are re-exported here so ``from ember_code.core.config.settings
# import PermissionsConfig, ...`` keeps working without downstream
# callers updating their import path.
from ember_code.core.config.schemas import (
    AgentsConfig,
    AuthConfig,
    CodeIndexConfig,
    ContextConfig,
    DisplayConfig,
    EvalsConfig,
    GuardrailsConfig,
    HooksConfig,
    KnowledgeConfig,
    LearningConfig,
    MemoryConfig,
    ModelsConfig,
    OrchestrationConfig,
    PermissionsConfig,
    ReasoningConfig,
    RulesConfig,
    SafetyConfig,
    SchedulerConfig,
    SkillsConfig,
    StorageConfig,
)
from ember_code.core.config.settings_loader import SettingsLoader


class Settings(BaseModel):
    """Complete igni settings."""

    api_url: str = "https://api.ignite-ember.sh"
    update_check_ttl: int = 86400
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    evals: EvalsConfig = Field(default_factory=EvalsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    code_index: CodeIndexConfig = Field(default_factory=CodeIndexConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)

    # TODO: Add telemetry config so users can wire their own Agno server or
    #       compatible telemetry endpoint (e.g. telemetry.enabled, telemetry.endpoint).
    #       Ember Cloud does not collect CLI telemetry — usage is tracked server-side.

    @classmethod
    def defaults(cls) -> Settings:
        """Return a fresh ``Settings`` populated entirely from the
        model's own ``Field`` defaults. Preferred over
        ``default_dict()`` when the caller wants typed access
        (e.g. ``Settings.defaults().models.registry`` is
        ``dict[str, dict[str, Any]]`` rather than string-indexed)."""
        return cls()

    @classmethod
    def default_dict(cls) -> dict[str, Any]:
        """Return the model's defaults as a plain dict. Used by
        :class:`SettingsLoader` to seed its accumulator — the merge
        pipeline runs on dicts, then ``finalize()`` validates the
        result back through ``Settings(**config)``."""
        return cls().model_dump()

    def has_usable_model(self, cloud_token: str | None = None) -> bool:
        """True when at least one registered model has usable credentials.

        Preflight for :class:`RunController`. A registered model is
        *usable* if any of the following holds:

        * its ``api_key`` is a non-empty string that isn't the
          ``cloud_token`` sentinel (i.e. a real inline key);
        * its ``api_key`` is the ``cloud_token`` sentinel AND
          ``cloud_token`` was passed in (or resolves via the
          fallback below);
        * it declares an ``api_key_env`` or ``api_key_cmd``
          fallback (either resolves later at model construction
          time).

        The cloud token is passed IN by the caller when they've
        already resolved it (Rule-2 fix — no inline
        :class:`CloudCredentials` import). When omitted, the method
        falls back to reading the credentials file via a lazy
        import so the class-level module import policy stays
        clean.
        """
        resolved_token = cloud_token
        if resolved_token is None:
            resolved_token = self._resolve_cloud_token()

        for cfg in self.models.registry.values():
            # Registry values are heterogeneous — cloud discovery
            # writes typed ``ModelRegistryEntry`` instances, user YAML
            # loads as raw dicts. Normalise both shapes through a
            # single ``ModelRegistryEntry`` coercion so the credential
            # check reads one flat set of attributes.
            entry = (
                cfg
                if isinstance(cfg, ModelRegistryEntry)
                else ModelRegistryEntry.model_validate(cfg)
            )
            key = entry.api_key or ""
            if key == CLOUD_TOKEN_SENTINEL and resolved_token:
                return True
            if key and key != CLOUD_TOKEN_SENTINEL:
                return True
            if entry.api_key_env or entry.api_key_cmd:
                return True
        return False

    def _resolve_cloud_token(self) -> str | None:
        """Owner of the one boundary crossing to :mod:`core.auth`.

        Isolated in a helper so :meth:`has_usable_model` — the only
        caller — can be tested by injecting ``cloud_token`` without
        touching the auth stack. The lazy import is the minimum
        surface needed to keep :mod:`core.config.settings` free of
        :mod:`core.auth` at module load (settings load happens
        before the auth stack initialises).
        """
        from ember_code.core.auth.credentials import CloudCredentials

        return CloudCredentials(self.auth.credentials_file).access_token


def load_settings(
    cli_overrides: CliOverrides | dict[str, Any] | None = None,
    project_dir: Path | None = None,
) -> Settings:
    """Load settings by merging the 5-tier precedence stack.

    Priority (highest first):

    1. Managed policy (sysadmin-controlled, OS-specific path)
    2. CLI flags
    3. .ember/config.local.yaml (project, gitignored)
    4. .ember/config.yaml (project, committed)
    5. ~/.ember/config.yaml (user global)
    6. Built-in defaults (from ``Settings``' Pydantic Field defaults)

    Managed sits ABOVE CLI on purpose — the whole point is that a
    user can't override an org policy by adding ``--auto-approve``
    on the command line. Same precedence ordering as Claude Code's
    managed > CLI > local > project > user stack.

    Thin wrapper around :class:`SettingsLoader`. Preserved as a
    module-level function because :mod:`cli.invocation` monkey-
    patches ``_settings_module.load_settings`` for tests — moving
    the entry point onto the class only would break those tests
    silently. This is the ONLY surviving module-level function; every
    other former shim now lives as a method on a class.
    """
    return SettingsLoader.load(cli_overrides=cli_overrides, project_dir=project_dir)


__all__ = [
    "AgentsConfig",
    "AuthConfig",
    "CodeIndexConfig",
    "ContextConfig",
    "DisplayConfig",
    "EvalsConfig",
    "GuardrailsConfig",
    "HooksConfig",
    "KnowledgeConfig",
    "LearningConfig",
    "MemoryConfig",
    "ModelsConfig",
    "OrchestrationConfig",
    "PermissionsConfig",
    "ReasoningConfig",
    "RulesConfig",
    "SafetyConfig",
    "SchedulerConfig",
    "Settings",
    "SettingsLoader",
    "SkillsConfig",
    "StorageConfig",
    "load_settings",
]
