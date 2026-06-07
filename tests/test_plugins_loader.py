"""Tests for the plugin loader: discovery, namespacing, apply.

These cover the Day-1 surface:

  - Four-root discovery + priority resolution.
  - Manifest parsing (well-formed, missing, malformed).
  - Bundled-contents inventory (``has_skills`` / ``has_agents`` flags).
  - Namespace application to ``SkillPool`` and ``AgentPool`` (the
    ``<plugin>:<name>`` rename).
  - ``disabled`` honoring at apply time.
  - State persistence roundtrip.

Hooks/MCP/tools loading lives in Day-2 tests (separate file).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ember_code.core.plugins.loader import PluginLoader
from ember_code.core.plugins.models import PluginManifest
from ember_code.core.plugins.state import (
    PluginsState,
    load_state,
    save_state,
    state_path,
)
from ember_code.core.skills.loader import SkillPool

# ── Helpers ─────────────────────────────────────────────────────────


def _write_plugin(
    root: Path,
    name: str,
    *,
    version: str | None = "1.0.0",
    description: str | None = None,
    with_skills: bool = False,
    with_agents: bool = False,
    with_hooks: bool = False,
    with_mcp: bool = False,
    with_tools: bool = False,
) -> Path:
    """Plant a Claude-Code-shaped plugin at ``root/<name>/``.

    Each ``with_*`` flag drops a minimal subdirectory or file so the
    loader's inventory pass picks it up. Skill / agent bodies are
    just enough to parse cleanly under the per-type loader.
    """
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name}
    if version is not None:
        manifest["version"] = version
    if description is not None:
        manifest["description"] = description
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    if with_skills:
        skill_dir = plugin_dir / "skills" / "demo"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo\n"
            "description: A demo skill bundled by the plugin\n"
            "category: development\n"
            "---\n\n"
            "Body.\n",
            encoding="utf-8",
        )

    if with_agents:
        (plugin_dir / "agents").mkdir(parents=True, exist_ok=True)
        (plugin_dir / "agents" / "demo.md").write_text(
            "---\n"
            "name: demo\n"
            "description: A demo agent bundled by the plugin\n"
            "tools: [Read]\n"
            "---\n\n"
            "System prompt body.\n",
            encoding="utf-8",
        )

    if with_hooks:
        hooks_dir = plugin_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text("{}", encoding="utf-8")

    if with_mcp:
        (plugin_dir / ".mcp.json").write_text("{}", encoding="utf-8")

    if with_tools:
        (plugin_dir / "tools").mkdir(parents=True, exist_ok=True)

    return plugin_dir


# ── Manifest parsing ────────────────────────────────────────────────


def test_manifest_requires_only_name(tmp_path: Path) -> None:
    """The only required field is ``name``. Version/description/author
    are all optional metadata — a barebones plugin must still load."""
    m = PluginManifest.model_validate({"name": "minimal"})
    assert m.name == "minimal"
    assert m.version is None
    assert m.description is None


def test_manifest_preserves_unknown_fields() -> None:
    """Future Claude Code manifest additions (e.g. ``keywords``,
    ``homepage``) must not break loading — we accept them via
    ``extra='allow'`` and just don't act on them yet."""
    m = PluginManifest.model_validate(
        {"name": "x", "homepage": "https://example.com", "keywords": ["a", "b"]}
    )
    assert m.name == "x"


# ── Discovery ───────────────────────────────────────────────────────


def test_discovers_plugin_with_manifest(tmp_path: Path) -> None:
    """Minimum viable plugin: a directory under one of the roots with
    a parseable ``.claude-plugin/plugin.json`` is registered."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "alpha")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    plugins = loader.list_plugins()
    assert [p.name for p in plugins] == ["alpha"]
    assert plugins[0].source.root == "user-ember"


def test_skips_directories_without_manifest(tmp_path: Path) -> None:
    """A folder under a plugin root that lacks
    ``.claude-plugin/plugin.json`` is ignored silently — leaves room
    for stray content, notes, gitkeeps, etc."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    user_ember.mkdir(parents=True)
    (user_ember / "not-a-plugin").mkdir()
    (user_ember / "not-a-plugin" / "readme.md").write_text("just a note")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")
    assert loader.list_plugins() == []


def test_warns_and_skips_malformed_manifest(tmp_path: Path) -> None:
    """A plugin with a malformed manifest is skipped (with a log warning,
    not a crash). One bad plugin shouldn't take down the whole load."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    plugin_dir = user_ember / "broken"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text("not-json-at-all", encoding="utf-8")
    _write_plugin(user_ember, "good")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")
    assert [p.name for p in loader.list_plugins()] == ["good"]


# ── Four-root priority ──────────────────────────────────────────────


def test_project_ember_wins_over_user_claude(tmp_path: Path) -> None:
    """Same-named plugin in two roots: project-ember (priority 4)
    beats user-claude (priority 1). This is the highest-vs-lowest
    pairing; intermediate priorities exercised in other tests."""
    user_claude = tmp_path / "home" / ".claude" / "plugins"
    project_ember = tmp_path / "proj" / ".ember" / "plugins"

    _write_plugin(user_claude, "shared", description="from user-claude")
    _write_plugin(project_ember, "shared", description="from project-ember")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    plugin = loader.get("shared")
    assert plugin is not None
    assert plugin.source.root == "project-ember"
    assert plugin.manifest.description == "from project-ember"


def test_project_claude_beats_user_ember(tmp_path: Path) -> None:
    """Project always beats user, even when the project version sits
    in ``.claude/`` and the user version sits in ``.ember/``. The
    project's voice wins regardless of which tool flavor it speaks."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    project_claude = tmp_path / "proj" / ".claude" / "plugins"

    _write_plugin(user_ember, "shared", description="from user-ember")
    _write_plugin(project_claude, "shared", description="from project-claude")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    plugin = loader.get("shared")
    assert plugin is not None
    assert plugin.source.root == "project-claude"


def test_user_ember_beats_user_claude(tmp_path: Path) -> None:
    """Within the user-global tier, ember beats claude. Mirrors the
    project tier's preference — if you bothered to install/maintain
    a plugin via ember, you want that copy used."""
    user_claude = tmp_path / "home" / ".claude" / "plugins"
    user_ember = tmp_path / "home" / ".ember" / "plugins"

    _write_plugin(user_claude, "shared", description="from user-claude")
    _write_plugin(user_ember, "shared", description="from user-ember")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    plugin = loader.get("shared")
    assert plugin is not None
    assert plugin.source.root == "user-ember"


# ── Bundled-contents inventory ──────────────────────────────────────


def test_inventory_flags_set_for_bundled_subdirs(tmp_path: Path) -> None:
    """The ``has_*`` flags drive the panel's per-plugin counts and let
    apply steps skip plugins that bundle nothing in a given category."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(
        user_ember,
        "kitchen-sink",
        with_skills=True,
        with_agents=True,
        with_hooks=True,
        with_mcp=True,
        with_tools=True,
    )
    _write_plugin(user_ember, "manifest-only")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    full = loader.get("kitchen-sink")
    assert full is not None
    assert full.has_skills and full.has_agents
    assert full.has_hooks and full.has_mcp and full.has_tools

    bare = loader.get("manifest-only")
    assert bare is not None
    assert not (
        bare.has_skills or bare.has_agents or bare.has_hooks or bare.has_mcp or bare.has_tools
    )


# ── Apply: SkillPool namespacing ────────────────────────────────────


def test_apply_to_skills_namespaces_loaded_skills(tmp_path: Path) -> None:
    """A plugin named ``foo`` whose ``skills/demo/SKILL.md`` ships
    ``name: demo`` lands in the SkillPool as ``foo:demo``. The
    original ``demo`` is unused — the prefix is the new identity, so
    a user-level ``demo`` skill in ``.ember/skills/`` and this
    plugin's ``demo`` can both exist."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "foo", with_skills=True)

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    pool = SkillPool()
    loader.apply_to_skills(pool)

    names = {s.name for s in pool.list_skills()}
    assert "foo:demo" in names
    assert "demo" not in names


def test_apply_to_skills_honors_disabled(tmp_path: Path) -> None:
    """A disabled plugin's skills never make it into the SkillPool —
    even though the plugin itself is still discovered (so the panel
    can show it as disabled)."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "off", with_skills=True)
    _write_plugin(user_ember, "on", with_skills=True)

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    pool = SkillPool()
    loader.apply_to_skills(pool, disabled={"off"})

    names = {s.name for s in pool.list_skills()}
    assert "on:demo" in names
    assert "off:demo" not in names
    # But the plugin itself stays visible to the panel.
    assert loader.get("off") is not None


# ── State persistence ───────────────────────────────────────────────


def test_state_roundtrip(tmp_path: Path) -> None:
    """Save then load returns equivalent state. The file lives at
    ``<data_dir>/plugins.json``; the atomic-write path goes via a
    ``.json.tmp`` sibling so a crash mid-write can't corrupt the
    canonical file."""
    state = PluginsState(disabled=["x", "y"], pins={"x": "abc123"})
    save_state(state, data_dir=tmp_path)

    loaded = load_state(data_dir=tmp_path)
    assert loaded.disabled == ["x", "y"]
    assert loaded.pins == {"x": "abc123"}
    # Path is what we expect — used by the installer to emit the
    # state-file location in error messages.
    assert state_path(data_dir=tmp_path) == tmp_path / "plugins.json"


def test_state_load_missing_returns_default(tmp_path: Path) -> None:
    """No file = fresh, empty state. Never raise for missing — every
    session of a brand-new ember install hits this path."""
    s = load_state(data_dir=tmp_path / "nothing-here")
    assert s.disabled == []
    assert s.pins == {}


def test_state_load_corrupt_returns_default(tmp_path: Path) -> None:
    """A corrupt state file logs a warning and falls back to default —
    the user can't recover broken pins, but at least the session
    starts. Otherwise every session would crash until they manually
    delete the file."""
    path = state_path(data_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage{not}json", encoding="utf-8")

    s = load_state(data_dir=tmp_path)
    assert s.disabled == []


# ── Project-dir default ────────────────────────────────────────────


def test_load_all_defaults_to_cwd(tmp_path: Path) -> None:
    """``project_dir=None`` falls back to ``Path.cwd()``. The CLI
    sometimes invokes the loader without an explicit project dir
    (e.g. during ``ember plugins list`` outside a project)."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "from-cwd")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all()  # no project_dir → cwd
    assert loader.get("from-cwd") is not None
