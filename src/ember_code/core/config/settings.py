"""Settings management with hierarchical config loading."""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ember_code.core.config.defaults import DEFAULT_CONFIG

logger = logging.getLogger(__name__)


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
    registry: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PermissionsConfig(BaseModel):
    # Legacy per-category levels — interpreted by the older
    # ``PermissionGuard``. Kept untouched for back-compat; the new
    # ``PermissionEvaluator`` reads ``mode`` / ``deny`` / ``ask`` /
    # ``allow`` instead.
    file_read: str = "allow"
    file_write: str = "ask"
    shell_execute: str = "ask"
    shell_restricted: str = "allow"
    web_search: str = "allow"
    web_fetch: str = "allow"
    git_push: str = "ask"
    git_destructive: str = "ask"
    # Claude Code-style permission system (mirrors
    # ``settings.json``'s ``permissions`` block). ``mode`` is one
    # of ``default`` / ``dontAsk`` / ``acceptEdits`` /
    # ``bypassPermissions`` / ``plan``. ``deny`` / ``ask`` /
    # ``allow`` are lists of ``Tool`` or ``Tool(pattern)`` strings
    # (e.g. ``"Bash(rm *)"``, ``"Read(./.env)"``).
    mode: str = "default"
    deny: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)


class SafetyConfig(BaseModel):
    protected_paths: list[str] = Field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            "credentials.*",
            "secrets.*",
        ]
    )
    blocked_commands: list[str] = Field(
        default_factory=lambda: [
            "rm -rf /",
            ":(){ :|:& };:",
        ]
    )
    max_file_size_kb: int = 500
    require_confirmation: list[str] = Field(
        default_factory=lambda: [
            "git push",
            "git push --force",
            "npm publish",
            "pip install",
            "docker run",
            "terraform apply",
            "kubectl apply",
            "kubectl delete",
        ]
    )


class StorageConfig(BaseModel):
    data_dir: str = "~/.ember"
    audit_log: str = "~/.ember/audit.log"
    max_history_runs: int = 10000


class RulesConfig(BaseModel):
    cross_tool_support: bool = True


class HooksConfig(BaseModel):
    cross_tool_support: bool = True


class ContextConfig(BaseModel):
    project_file: str = "ember.md"
    ignore_patterns: list[str] = Field(
        default_factory=lambda: [
            "node_modules/",
            ".git/",
            "__pycache__/",
            "*.pyc",
            ".venv/",
            "dist/",
            "build/",
        ]
    )


class OrchestrationConfig(BaseModel):
    max_nesting_depth: int = 5
    max_total_agents: int = 20
    # Per-specialist deadline. 10 minutes was too aggressive for
    # reasoning-heavy broadcasts (security audits, large refactors)
    # where each specialist can chew through many tool calls. Bump
    # to 30m — long enough for a thorough analysis, short enough
    # that a hung model provider still gets killed before the
    # session feels frozen.
    sub_team_timeout: int = 1800
    max_task_iterations: int = 10
    generate_ephemeral: bool = True
    max_ephemeral_per_session: int = 5
    auto_cleanup: bool = True


class AgentsConfig(BaseModel):
    cross_tool_support: bool = True


class SkillsConfig(BaseModel):
    cross_tool_support: bool = True
    auto_trigger: bool = True
    default_agent: str = "editor"


class MemoryConfig(BaseModel):
    add_memories_to_context: bool = True


class KnowledgeConfig(BaseModel):
    enabled: bool = True
    collection_name: str = "ember_knowledge"
    max_results: int = 10
    # ── Git-shared knowledge ──────────────────────────────────────
    share: bool = True  # enable git-synced knowledge sharing
    share_file: str = ".ember/knowledge.yaml"  # path relative to project root
    auto_sync: bool = True  # auto-sync on session start/end


class LearningConfig(BaseModel):
    enabled: bool = True
    # Auto-extraction blobs that Agno's LearningMachine fires *after*
    # every run as separate LLM calls. They added 5–10 s to the tail
    # between ``streaming_done`` and ``run_completed`` (the user
    # perceives "still working" while the visible answer is already
    # done). We rely on the agentic ``user_memory`` path instead —
    # the agent calls ``update_user_memory`` itself when it decides
    # the turn was memorable — so the auto-extractions are dead
    # weight in our setup.
    user_profile: bool = False
    user_memory: bool = True
    session_context: bool = False
    entity_memory: bool = False
    learned_knowledge: bool = False


class ReasoningConfig(BaseModel):
    enabled: bool = False
    add_instructions: bool = True
    add_few_shot: bool = False


class GuardrailsConfig(BaseModel):
    pii_detection: bool = True
    prompt_injection: bool = False
    moderation: bool = False


class EvalsConfig(BaseModel):
    judge_model: str = "MiniMax-M2.7"
    num_iterations: int = 3
    accuracy_threshold: float = 7.0
    timeout_per_case: int = 30


class SchedulerConfig(BaseModel):
    poll_interval: int = 30
    task_timeout: int = 300
    max_concurrent: int = 1


class AuthConfig(BaseModel):
    credentials_file: str = "~/.ember/credentials.json"


class CodeIndexConfig(BaseModel):
    """Tunables for the local code-index sync.

    ``repository_id`` and the GCS bucket are auto-discovered from
    ``settings.api_url`` using the local git remote — users don't
    configure either.
    """

    fetch_timeout: float = 60.0


class DisplayConfig(BaseModel):
    markdown: bool = True
    show_tool_calls: bool = True
    show_routing: bool = False
    show_reasoning: bool = False
    color_theme: str = "auto"
    tool_result_preview_lines: int = 4
    message_truncate_lines: int = 10


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


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base, returning new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict:
    """Load YAML file, returning empty dict if not found."""
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    return {}


def save_default_model(model_name: str) -> None:
    """Persist a model choice to ``~/.ember/config.yaml`` so it
    survives across app restarts.

    Was missing entirely — the picker / ``/model <name>`` flows
    only flipped ``settings.models.default`` in memory, so the
    next launch always loaded the built-in default and the user
    had to re-pick every session. Writes a minimal patch:

    * reads the existing user config (or starts blank)
    * sets/updates ``models.default``
    * writes back via ``yaml.safe_dump``

    The hosted-model *registry* is intentionally NOT persisted
    here — it gets refreshed from cloud discovery on session
    start, so freezing it would just stale-out as new models
    ship. Only the default identity is sticky.
    """
    user_config_path = Path.home() / ".ember" / "config.yaml"
    existing = _load_yaml(user_config_path)
    models_block = existing.setdefault("models", {})
    if not isinstance(models_block, dict):
        models_block = {}
        existing["models"] = models_block
    models_block["default"] = model_name
    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(user_config_path, "w") as f:
        yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=False)


_EMBER_CLOUD_HOST = "api.ignite-ember.sh"


def _is_ember_cloud_url(url: str) -> bool:
    """True only when the URL points at the production cloud host.

    Hostname-exact (not substring) so dev/staging overrides like
    ``dev-api.ignite-ember.sh`` are treated as user-managed and survive
    the migration step. Without the exact match, a substring check would
    flag dev URLs as cloud and clobber them back to prod.
    """
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname == _EMBER_CLOUD_HOST
    except Exception:
        return False


def _migrate_cloud_models(config: dict[str, Any]) -> None:
    """Override Ember Cloud models in user config with latest defaults.

    Any model whose url points at the production cloud host is managed
    by us, so we replace it with the current default to ensure users
    always get the latest model after upgrading.
    """
    default_registry = DEFAULT_CONFIG.get("models", {}).get("registry", {})
    user_registry = config.get("models", {}).get("registry", {})

    if not user_registry:
        return

    # Build a lookup of default cloud models by url
    default_cloud = {}
    for name, entry in default_registry.items():
        if _is_ember_cloud_url(entry.get("url", "")):
            default_cloud[name] = entry

    # Replace user's cloud models with latest defaults
    for name in list(user_registry):
        entry = user_registry[name]
        if _is_ember_cloud_url(entry.get("url", "")):
            if name in default_cloud:
                # Update existing entry with latest defaults
                user_registry[name] = {**default_cloud[name]}
            else:
                # Old cloud model not in defaults anymore — replace with
                # the current default model
                default_name = config.get("models", {}).get("default", "")
                if default_name in default_cloud:
                    user_registry[name] = {**default_cloud[default_name]}
                    user_registry[name]["model_id"] = default_cloud[default_name]["model_id"]

    # Ensure the default model exists in the registry
    default_model = config.get("models", {}).get("default", "")
    if default_model and default_model not in user_registry and default_model in default_cloud:
        user_registry[default_model] = {**default_cloud[default_model]}


def _platform_managed_settings_path() -> Path | None:
    """OS-specific path for the sysadmin-enforced managed policy file.

    Mirrors Claude Code's managed-settings tier — a write-protected
    location that overrides every other layer including CLI flags.
    The intent is that a sysadmin (or MDM profile) drops a YAML file
    here to enforce org-wide policy (e.g. ``permissions.mode: dontAsk``,
    a pinned model, a blocked-commands list) that a user can't disable
    just by adding a `--strict` flag or editing project config.

    The file format is YAML (also accepts JSON, since JSON is a strict
    subset of YAML). Returns ``None`` on unknown platforms — the
    loader treats that as "no managed tier."
    """
    import sys

    if sys.platform == "darwin":
        return Path("/Library/Application Support/Ember/managed-settings.yaml")
    if sys.platform.startswith("linux"):
        return Path("/etc/ember/managed-settings.yaml")
    if sys.platform == "win32":
        import os

        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "Ember" / "managed-settings.yaml"
    return None


def _load_managed_settings() -> dict:
    """Load the managed-settings YAML/JSON file if one is deployed.

    Thin wrapper so tests can monkey-patch the platform path lookup
    without reaching into ``load_settings``. Returns ``{}`` when no
    file exists or the platform has no defined managed-settings
    location.
    """
    path = _platform_managed_settings_path()
    if path is None:
        return {}
    return _load_yaml(path)


# Top-level keys we lift from ``settings.json`` into the unified
# ``Settings`` config. Other keys (notably ``hooks``) are owned by
# dedicated loaders that read ``settings.json`` themselves — we
# don't double-import them here. ``permissions`` is the one block
# the CC-style settings file shares with the YAML config and
# without lifting it the PermissionEvaluator never sees user-tier
# deny rules (only ``ToolPermissions`` did, and it gates
# ``requires_confirmation`` rather than blocking the call).
_JSON_KEYS_TO_LIFT = ("permissions",)


def _load_settings_json_fragment(path: Path) -> dict:
    """Read ``settings.json`` at ``path`` and return ONLY the keys
    we want merged into the unified config. Silently empty on
    missing file or parse error — best-effort, mirrors the YAML
    loader's behavior.
    """
    if not path.exists():
        return {}
    try:
        import json as _json

        data = _json.loads(path.read_text())
    except Exception as exc:
        logger.debug("settings.json load failed at %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _JSON_KEYS_TO_LIFT if k in data}


def load_settings(
    cli_overrides: dict[str, Any] | None = None,
    project_dir: Path | None = None,
) -> Settings:
    """Load settings by merging the 5-tier precedence stack.

    Priority (highest first):
    1. Managed policy (sysadmin-controlled, OS-specific path)
    2. CLI flags
    3. .ember/config.local.yaml (project, gitignored)
    4. .ember/config.yaml (project, committed)
    5. ~/.ember/config.yaml (user global)
    6. Built-in defaults

    Managed sits ABOVE CLI on purpose — the whole point is that a
    user can't override an org policy by adding ``--auto-approve``
    on the command line. Same precedence ordering as Claude Code's
    managed > CLI > local > project > user stack.
    """
    config = DEFAULT_CONFIG.copy()

    # User global config
    user_config_path = Path.home() / ".ember" / "config.yaml"
    config = _deep_merge(config, _load_yaml(user_config_path))
    # User global settings.json — CC-style file. We lift only the
    # ``permissions`` block here; ``hooks`` is owned by its own
    # loader. Without this, a ``permissions.deny`` rule the user
    # wrote in ``~/.ember/settings.json`` never reached the
    # ``PermissionEvaluator`` and bypass mode silently allowed
    # commands the rule should have blocked.
    config = _deep_merge(
        config, _load_settings_json_fragment(Path.home() / ".ember" / "settings.json")
    )
    config = _deep_merge(
        config,
        _load_settings_json_fragment(Path.home() / ".ember" / "settings.local.json"),
    )

    # Project config
    if project_dir is None:
        project_dir = Path.cwd()

    project_config = project_dir / ".ember" / "config.yaml"
    config = _deep_merge(config, _load_yaml(project_config))
    config = _deep_merge(
        config, _load_settings_json_fragment(project_dir / ".ember" / "settings.json")
    )

    # Project local config (gitignored)
    project_local = project_dir / ".ember" / "config.local.yaml"
    config = _deep_merge(config, _load_yaml(project_local))
    config = _deep_merge(
        config,
        _load_settings_json_fragment(project_dir / ".ember" / "settings.local.json"),
    )

    # CLI overrides
    if cli_overrides:
        config = _deep_merge(config, cli_overrides)

    # Managed policy — last, so it wins over CLI. Sysadmin-controlled
    # path that users can't write to without elevated privileges, by
    # design (it's the "you can't ``--auto-approve`` your way out of
    # the org policy" tier).
    config = _deep_merge(config, _load_managed_settings())

    # Migrate Ember Cloud models to latest defaults
    _migrate_cloud_models(config)

    return Settings(**config)
