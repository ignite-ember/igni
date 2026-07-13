"""Project initializer and updater for .ember directory.

Two responsibilities:
1. **First-run init** — copies built-in agents, skills, hooks into `.ember/`
   and creates a starter `ember.md`.  Marker file ensures this runs once.
2. **Update on every start** — compares package files against local copies
   using checksums.  Overwrites untouched files, warns about modified ones.
"""

import logging
import stat
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.init_checksums import (
    load_checksums,
    save_checksums,
    sync_file,
)
from ember_code.core.init_json_io import load_json, save_json
from ember_code.core.init_templates import (
    CONFIG_YAML_HEADER,
    EMBER_MD_TEMPLATE,
    POST_COMMIT_TODO_HOOK,
    PRE_PR_REVIEW_HOOK,
    PROJECT_CONFIG_TEMPLATE,
    _HOME_CONFIG_BOOTSTRAP,
)

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────

PACKAGE_DIR = Path(__file__).parent.parent  # src/ember_code
MARKER_FILE = ".initialized"
CHECKSUMS_FILE = ".checksums.json"


# ── Built-in hook scripts ─────────────────────────────────────────────

class HookDefinition(BaseModel):
    """Settings-file shape for one hook registration.

    Serialised into ``.ember/settings.json`` via ``model_dump``. Uses
    ``populate_by_name`` so callers can still pass ``type`` — which is
    a Python builtin — as a keyword argument, while the field is
    named ``kind`` in Python code and re-aliased back to ``type`` on
    the wire (Rule 1 — no dict literals; see [[project_docs_status]]
    for the general shape used by other settings payloads).
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    kind: str = Field(alias="type")
    command: str
    matcher: str
    timeout: int
    background: bool = False


class BuiltInHookSpec(BaseModel):
    """One built-in hook shipped by the package.

    ``content`` is the script body, written to
    ``.ember/hooks/<filename>`` and marked executable during
    :func:`_provision_hooks`. ``definition`` is registered in
    ``settings.json`` under the ``event`` key.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    content: str
    event: str
    definition: HookDefinition


BUILT_IN_HOOKS: tuple[BuiltInHookSpec, ...] = (
    BuiltInHookSpec(
        filename="pre-pr-review.sh",
        content=PRE_PR_REVIEW_HOOK,
        event="PreToolUse",
        definition=HookDefinition(
            type="command",
            command=".ember/hooks/pre-pr-review.sh",
            matcher="Bash",
            timeout=15000,
        ),
    ),
    BuiltInHookSpec(
        filename="post-commit-todo.sh",
        content=POST_COMMIT_TODO_HOOK,
        event="PostToolUse",
        definition=HookDefinition(
            type="command",
            command=".ember/hooks/post-commit-todo.sh",
            matcher="Bash",
            timeout=15000,
            background=True,
        ),
    ),
)



# ── Public API ────────────────────────────────────────────────────────


def initialize_project(project_dir: Path) -> bool:
    """Initialize and update the project's .ember directory.

    First run: copies built-in agents, skills, hooks, creates ember.md.
    Subsequent runs: updates built-in files using checksum-based merge:
      - Untouched files → overwritten with new package version
      - User-modified files → kept, warning logged
      - New package files → copied
      - User's custom files → never deleted
    """
    home_ember = Path.home() / ".ember"
    home_ember.mkdir(parents=True, exist_ok=True)
    home_marker = home_ember / MARKER_FILE
    project_marker = project_dir / ".ember" / MARKER_FILE

    ember_dir = project_dir / ".ember"
    ember_dir.mkdir(parents=True, exist_ok=True)

    # Write home config if missing (user-global, first-ever run)
    if not home_marker.exists():
        _write_default_config(home_ember)
        home_marker.touch()

    # Migrate stale defaults from older versions — runs every startup.
    # Cheap and idempotent (a no-op once migrated).
    _migrate_home_model_default(home_ember)

    # First-time project init: create starter files
    first_run = not project_marker.exists()
    if first_run:
        _write_ember_md(project_dir)
        _write_project_config(project_dir)
        _write_project_settings(project_dir)
        project_marker.touch()

    # Sync built-in agents/skills — checksum-based so user edits are preserved
    warnings = _update_built_in_files(project_dir)
    _provision_hooks(project_dir)
    for msg in warnings:
        logger.info(msg)

    return first_run


# ── Checksum-based update ────────────────────────────────────────────


def _update_built_in_files(project_dir: Path) -> list[str]:
    """Sync built-in agents and skills using checksum-based merge.

    Returns a list of warning messages for files that were modified by the
    user and could not be auto-updated.
    """
    checksums = load_checksums(project_dir)
    warnings: list[str] = []

    # Update agents
    agents_src = PACKAGE_DIR / "bundled_agents"
    agents_dst = project_dir / ".ember" / "agents"
    if agents_src.exists():
        agents_dst.mkdir(parents=True, exist_ok=True)
        for src_file in agents_src.glob("*.md"):
            key = f"agents/{src_file.name}"
            dst_file = agents_dst / src_file.name
            warn = sync_file(src_file, dst_file, key, checksums)
            if warn:
                warnings.append(warn)

    # Update skills
    skills_src = PACKAGE_DIR / "bundled_skills"
    skills_dst = project_dir / ".ember" / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            src_file = skill_dir / "SKILL.md"
            if not src_file.exists():
                continue
            key = f"skills/{skill_dir.name}/SKILL.md"
            dst_dir = skills_dst / skill_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_file = dst_dir / "SKILL.md"
            warn = sync_file(src_file, dst_file, key, checksums)
            if warn:
                warnings.append(warn)

    save_checksums(project_dir, checksums)
    return warnings


def _write_default_config(home_ember: Path) -> None:
    """Write a minimal starter config.yaml if one doesn't exist.

    Earlier versions dumped the whole ``DEFAULT_CONFIG`` here, which
    duplicated the bundled cloud model entry into every client's home
    file and made model rollouts a per-user migration headache. The
    bootstrap is now intentionally empty — users fill it in with
    overrides as they go, and cloud discovery handles the hosted
    catalogue automatically.
    """
    config_path = home_ember / "config.yaml"
    if not config_path.exists():
        config_path.write_text(CONFIG_YAML_HEADER + _HOME_CONFIG_BOOTSTRAP)


def _migrate_home_model_default(home_ember: Path) -> None:
    """Remove legacy bundled cloud entries from ``~/.ember/config.yaml``.

    Older versions wrote a copy of the bundled Ember Cloud model into
    every client's home config. The cloud catalogue is now fetched on
    each session start (see :py:func:`fetch_cloud_models`), so the
    home file should only carry the user's own overrides. This pass
    strips any registry entry that looks like a bundled cloud entry
    (Ember Cloud URL + ``cloud_token`` api_key) and clears the
    ``default`` field when it names one of those entries. User-edited
    entries — different URL, custom api_key, custom model_id — are
    left untouched.

    Idempotent: once the bundled rows are gone, subsequent runs are
    no-ops.
    """
    config_path = home_ember / "config.yaml"
    if not config_path.exists():
        return
    try:
        data = yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        logger.debug("Skipping model migration: home config unreadable.")
        return
    if not isinstance(data, dict):
        return

    models = data.get("models")
    if not isinstance(models, dict):
        return
    registry = models.get("registry")
    if not isinstance(registry, dict):
        registry = {}

    removed: list[str] = []
    for name in list(registry.keys()):
        entry = registry[name]
        if isinstance(entry, dict) and _looks_like_bundled_cloud_entry(entry):
            del registry[name]
            removed.append(name)

    # If the active default named one of the just-removed entries,
    # clear it so cloud discovery (or the resolver fallback) picks
    # something current.
    if models.get("default") in removed:
        models["default"] = ""

    if not removed:
        return

    # Tidy: empty registry → omit the key entirely; empty default
    # string → same. Keeps the file as small as possible after
    # migration. If ``models`` itself ends up empty, drop it too.
    if not registry:
        models.pop("registry", None)
    if models.get("default", None) == "":
        models.pop("default", None)
    if not models:
        data.pop("models", None)

    try:
        config_path.write_text(
            CONFIG_YAML_HEADER + yaml.dump(data, default_flow_style=False, sort_keys=False)
        )
        logger.info(
            "Migrated ~/.ember/config.yaml: removed legacy bundled cloud entries %s.",
            removed,
        )
    except Exception:
        logger.warning(
            "Failed to write migrated home config — bundled cloud "
            "entries may still shadow the live catalogue. Edit "
            "~/.ember/config.yaml manually if so.",
            exc_info=True,
        )


def _looks_like_bundled_cloud_entry(entry: dict[str, object]) -> bool:
    """True for entries that match the old bundled cloud-model shape.

    The signal is ``url`` pointing at an ``ignite-ember.sh`` host AND
    ``api_key == "cloud_token"``. Both anchor on conventions only the
    package ever wrote; a user pointing their own ``cloud_token``
    sentinel at a different URL (or vice-versa) doesn't match.
    """
    url = entry.get("url")
    if not isinstance(url, str) or "ignite-ember.sh" not in url:
        return False
    return entry.get("api_key") == "cloud_token"


def _provision_hooks(project_dir: Path) -> None:
    """Write built-in hook scripts and register them in settings.

    Hook scripts are always overwritten (they are not user-customizable
    in the same way agents/skills are — users configure hooks via
    settings.json, not by editing the scripts).
    """
    hooks_dir = project_dir / ".ember" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    settings_path = project_dir / ".ember" / "settings.json"
    settings = load_json(settings_path)

    for hook in BUILT_IN_HOOKS:
        # Write the hook script (always overwrite — hooks are code, not config)
        script_path = hooks_dir / hook.filename
        script_path.write_text(hook.content)
        script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Register in settings (skip if already registered). ``by_alias=True``
        # renders the model with ``type`` on the wire — that's what the
        # settings-file schema expects; ``kind`` is only the Python name.
        definition = hook.definition.model_dump(by_alias=True)
        event_hooks = settings.setdefault("hooks", {}).setdefault(hook.event, [])
        if not any(h.get("command") == definition["command"] for h in event_hooks):
            event_hooks.append(definition)

    save_json(settings_path, settings)


def _write_ember_md(project_dir: Path) -> None:
    """Write a starter ember.md if one doesn't exist."""
    path = project_dir / "ember.md"
    if not path.exists():
        path.write_text(EMBER_MD_TEMPLATE)


def _write_project_config(project_dir: Path) -> None:
    """Write a starter .ember/config.yaml with commented-out options."""
    path = project_dir / ".ember" / "config.yaml"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PROJECT_CONFIG_TEMPLATE)


# Default permissions for new projects. Use display names only —
# tool_permissions.py:FUNC_TO_TOOL normalizes Agno function names
# (``run_shell_command``, ``edit_file``, ``save_file``, …) to the
# display name (``Bash``, ``Edit``, ``Write``) before any rule lookup,
# so listing both is redundant and leaks an internal detail into a
# user-facing config file.
DEFAULT_PERMISSIONS = {
    "allow": ["Glob", "Grep", "LS", "Read", "WebSearch", "WebFetch"],
    "ask": ["Write", "Edit", "Bash", "BashOutput", "Python"],
}


def _write_project_settings(project_dir: Path) -> None:
    """Write a starter .ember/settings.local.json with default permissions.

    This gives users a template they can customize for their project.
    The file is gitignored so each user can have their own overrides.
    Team defaults can go in .ember/settings.json if needed (committed).
    """
    path = project_dir / ".ember" / "settings.local.json"
    settings = load_json(path)

    # Only add permissions if not already present
    if "permissions" not in settings:
        settings["permissions"] = DEFAULT_PERMISSIONS
        save_json(path, settings)

