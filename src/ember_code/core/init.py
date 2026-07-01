"""Project initializer and updater for .ember directory.

Two responsibilities:
1. **First-run init** — copies built-in agents, skills, hooks into `.ember/`
   and creates a starter `ember.md`.  Marker file ensures this runs once.
2. **Update on every start** — compares package files against local copies
   using checksums.  Overwrites untouched files, warns about modified ones.
"""

import hashlib
import json
import logging
import shutil
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────

PACKAGE_DIR = Path(__file__).parent.parent  # src/ember_code
MARKER_FILE = ".initialized"
CHECKSUMS_FILE = ".checksums.json"


# ── Built-in hook scripts ─────────────────────────────────────────────

PRE_PR_REVIEW_HOOK = """\
#!/bin/bash
# .ember/hooks/pre-pr-review.sh
# Hook: PreToolUse (matcher: Bash)
#
# Early warning before push/PR: detects TODOs, debug statements, and
# console.log in staged changes. Informs the AI so it can fix them
# before the push proceeds.

# Read payload from stdin
payload=$(cat)
cmd=$(echo "$payload" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"command"[[:space:]]*:[[:space:]]*"//;s/"$//')

# Only check push/PR commands
case "$cmd" in
  *"git push"*|*"gh pr create"*|*"gh pr"*) ;;
  *) echo '{"continue": true}'; exit 0 ;;
esac

# Check for leftover debug/TODO in staged changes
diff_output=$(git diff --cached 2>/dev/null || git diff HEAD 2>/dev/null)
issues=()

todo_count=$(echo "$diff_output" | grep "^+" | grep -c -i "TODO\\|FIXME\\|HACK\\|XXX" || true)
todo_count=$(echo "$todo_count" | tr -d '[:space:]')
[[ "$todo_count" -gt 0 ]] 2>/dev/null && issues+=("$todo_count TODO/FIXME comment(s)")

debug_count=$(echo "$diff_output" | grep "^+" | grep -c "console\\.log\\|debugger\\|breakpoint()\\|import pdb\\|print(" || true)
debug_count=$(echo "$debug_count" | tr -d '[:space:]')
[[ "$debug_count" -gt 0 ]] 2>/dev/null && issues+=("$debug_count debug statement(s)")

if [[ ${#issues[@]} -eq 0 ]]; then
  echo '{"continue": true}'
  exit 0
fi

msg=$(IFS=", "; echo "${issues[*]}")
echo "{\\"continue\\": true, \\"systemMessage\\": \\"Before pushing: found ${msg} in your changes. Review and fix these issues before proceeding with the push.\\"}"
exit 0
"""

POST_COMMIT_TODO_HOOK = """\
#!/bin/bash
# .ember/hooks/post-commit-todo.sh
# Hook: PostToolUse (matcher: Bash, background: true)
#
# After a git commit, feeds the commit context to the AI so it can
# intelligently update .ember/TODO.md — crossing out completed items
# and adding new ones based on what the commit actually did.

payload=$(cat)
cmd=$(echo "$payload" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"command"[[:space:]]*:[[:space:]]*"//;s/"$//')

# Only act on commit commands
case "$cmd" in
  *"git commit"*) ;;
  *) echo '{"continue": true}'; exit 0 ;;
esac

# Only if TODO.md exists
if [[ ! -f ".ember/TODO.md" ]]; then
  echo '{"continue": true}'
  exit 0
fi

# Gather commit context
commit_msg=$(git log -1 --pretty=format:"%s" 2>/dev/null)
files_changed=$(git diff HEAD~1..HEAD --stat 2>/dev/null | head -30)
diff_preview=$(git diff HEAD~1..HEAD 2>/dev/null | head -200)

# Build the system message
msg="A git commit was just made. Review it and update .ember/TODO.md:\\n"
msg+="- Mark completed items as done (change '- [ ]' to '- [x]')\\n"
msg+="- Add new items if the commit introduced incomplete work\\n"
msg+="- Remove items that are no longer relevant\\n\\n"
msg+="Commit: ${commit_msg}\\n\\n"
msg+="Files changed:\\n${files_changed}\\n\\n"
msg+="Diff preview:\\n${diff_preview}"

# Use python to safely JSON-encode the message
escaped=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$msg")

echo "{\\"continue\\": true, \\"systemMessage\\": ${escaped}}"
exit 0
"""

BUILT_IN_HOOKS = [
    {
        "filename": "pre-pr-review.sh",
        "content": PRE_PR_REVIEW_HOOK,
        "event": "PreToolUse",
        "definition": {
            "type": "command",
            "command": ".ember/hooks/pre-pr-review.sh",
            "matcher": "Bash",
            "timeout": 15000,
        },
    },
    {
        "filename": "post-commit-todo.sh",
        "content": POST_COMMIT_TODO_HOOK,
        "event": "PostToolUse",
        "definition": {
            "type": "command",
            "command": ".ember/hooks/post-commit-todo.sh",
            "matcher": "Bash",
            "timeout": 15000,
            "background": True,
        },
    },
]

# ── Starter ember.md template ─────────────────────────────────────────

EMBER_MD_TEMPLATE = """\
# Project Context

<!-- This file gives igni agents context about your project.
     Edit it to match your project's specifics. Agents read this file
     before every task to understand conventions, architecture, and
     domain terminology. -->

## Overview

<!-- Brief description of what this project does. -->

## Tech Stack

<!-- Languages, frameworks, key libraries. -->

## Architecture

<!-- High-level structure: key directories, module boundaries, data flow. -->

## Conventions

<!-- Naming, formatting, patterns the team follows. -->

## Domain Terminology

<!-- Project-specific terms and their meanings. -->
"""


CONFIG_YAML_HEADER = """\
# igni — user configuration
# This file lives at ~/.ember/config.yaml and is never committed to git.
# Project-level overrides go in .ember/config.yaml inside your repo.
# See https://docs.ignite-ember.sh/configuration for details.

"""

PROJECT_CONFIG_TEMPLATE = """\
# igni — project configuration
# This file can be committed to git. Team members share these settings.
# User-level overrides go in ~/.ember/config.yaml.
# See https://docs.ignite-ember.sh/configuration for details.

# models:
#   default: MiniMax-M2.7        # Default model for this project

guardrails:
  pii_detection: true             # Warn on PII in user messages
  # prompt_injection: false       # Warn on prompt injection patterns

knowledge:
  enabled: true                   # Weaviate-backed knowledge base
  collection_name: ember_knowledge

learning:
  enabled: true                   # Learn user preferences, project context, entities across sessions

# orchestration:
#   max_nesting_depth: 5          # Max recursive sub-team levels
#   max_total_agents: 20          # Max agents per request
#   sub_team_timeout: 600         # Sub-team kill timeout (seconds)
"""


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


def _file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file's content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _load_checksums(project_dir: Path) -> dict[str, str]:
    """Load .ember/.checksums.json — maps relative paths to original hashes."""
    path = project_dir / ".ember" / CHECKSUMS_FILE
    return _load_json(path)


def _save_checksums(project_dir: Path, checksums: dict[str, str]) -> None:
    """Save .ember/.checksums.json."""
    path = project_dir / ".ember" / CHECKSUMS_FILE
    _save_json(path, checksums)


def _update_built_in_files(project_dir: Path) -> list[str]:
    """Sync built-in agents and skills using checksum-based merge.

    Returns a list of warning messages for files that were modified by the
    user and could not be auto-updated.
    """
    checksums = _load_checksums(project_dir)
    warnings: list[str] = []

    # Update agents
    agents_src = PACKAGE_DIR / "bundled_agents"
    agents_dst = project_dir / ".ember" / "agents"
    if agents_src.exists():
        agents_dst.mkdir(parents=True, exist_ok=True)
        for src_file in agents_src.glob("*.md"):
            key = f"agents/{src_file.name}"
            dst_file = agents_dst / src_file.name
            warn = _sync_file(src_file, dst_file, key, checksums)
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
            warn = _sync_file(src_file, dst_file, key, checksums)
            if warn:
                warnings.append(warn)

    _save_checksums(project_dir, checksums)
    return warnings


def _sync_file(src: Path, dst: Path, key: str, checksums: dict[str, str]) -> str | None:
    """Sync a single built-in file. Returns a warning string or None.

    Logic:
      - dst doesn't exist → copy, record checksum
      - no stored checksum (legacy) → record current package hash, skip update
      - package unchanged → skip
      - package changed + user didn't modify → overwrite, update checksum
      - package changed + user modified → skip, return warning
    """
    pkg_hash = _file_hash(src)
    stored_hash = checksums.get(key)

    if not dst.exists():
        # New file — copy and record
        shutil.copy2(src, dst)
        checksums[key] = pkg_hash
        return None

    if stored_hash is None:
        # Legacy: file exists but no checksum recorded.
        # Record current package hash so future updates work.
        checksums[key] = pkg_hash
        return None

    if pkg_hash == stored_hash:
        # Package hasn't changed — nothing to do
        return None

    # Package has changed — check if user modified their copy
    local_hash = _file_hash(dst)

    if local_hash == stored_hash:
        # User hasn't touched it — safe to overwrite
        shutil.copy2(src, dst)
        checksums[key] = pkg_hash
        return None

    # User modified AND package updated — write new version alongside
    new_path = dst.with_suffix(dst.suffix + ".new")
    shutil.copy2(src, new_path)
    checksums[key] = pkg_hash
    return (
        f"Built-in {key} was updated but you have local modifications. "
        f"New version saved as .ember/{key}.new — diff and merge at your convenience."
    )


# ── Internal helpers ──────────────────────────────────────────────────


_HOME_CONFIG_BOOTSTRAP = """\
# Personal overrides — only what differs from package defaults belongs
# here. Hosted models come from cloud discovery on session start (see
# https://docs.ignite-ember.sh/configuration) so you don't need to
# declare them; this file is for your own additions:
#
# models:
#   # Pin a different default than the first cloud model:
#   # default: gpt-4o
#   registry:
#     # Your own provider — uses an env-var-resolved API key:
#     # gpt-4o:
#     #   provider: openai_like
#     #   model_id: gpt-4o
#     #   api_key_env: OPENAI_API_KEY
"""


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
    import yaml

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
    settings = _load_json(settings_path)

    for hook in BUILT_IN_HOOKS:
        # Write the hook script (always overwrite — hooks are code, not config)
        filename = str(hook["filename"])
        script_path = hooks_dir / filename
        script_path.write_text(str(hook["content"]))
        script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Register in settings (skip if already registered)
        event = hook["event"]
        definition: dict[str, object] = dict(hook["definition"])  # type: ignore[arg-type]
        event_hooks = settings.setdefault("hooks", {}).setdefault(event, [])
        if not any(h.get("command") == definition["command"] for h in event_hooks):
            event_hooks.append(definition)

    _save_json(settings_path, settings)


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
    settings = _load_json(path)

    # Only add permissions if not already present
    if "permissions" not in settings:
        settings["permissions"] = DEFAULT_PERMISSIONS
        _save_json(path, settings)


def _load_json(path: Path) -> dict:
    """Load a JSON file, returning empty dict if missing or invalid."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    """Write a dict as formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
