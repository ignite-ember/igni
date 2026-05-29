"""Tests for Day-2 plugin apply paths: hooks, MCP, custom tools.

Covers ``PluginLoader.apply_to_hooks``, ``apply_to_mcp``, and
``collect_tool_dirs``, plus the per-loader plumbing on
``HookLoader.load_plugin_hooks`` and
``MCPConfigLoader.load_plugin_servers``.

The Day-1 file already covers manifest parsing, four-root priority,
and the skills/agents apply paths. This file focuses on the three
new categories — separated so a regression in one doesn't drag the
other test classes into a noisy failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ember_code.core.hooks.loader import HookLoader
from ember_code.core.hooks.schemas import HookDefinition
from ember_code.core.mcp.config import MCPConfigLoader, MCPServerConfig
from ember_code.core.plugins.loader import PluginLoader

# ── Helpers ─────────────────────────────────────────────────────────


def _write_plugin_with_hooks(root: Path, name: str, hooks_block: dict) -> Path:
    """Plant a plugin whose ``hooks/hooks.json`` carries *hooks_block*."""
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    (plugin_dir / "hooks").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "hooks" / "hooks.json").write_text(json.dumps(hooks_block), encoding="utf-8")
    return plugin_dir


def _write_plugin_with_mcp(
    root: Path, name: str, mcp_block: dict, *, filename: str = ".mcp.json"
) -> Path:
    """Plant a plugin whose plugin-root ``.mcp.json`` carries *mcp_block*."""
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    (plugin_dir / filename).write_text(json.dumps(mcp_block), encoding="utf-8")
    return plugin_dir


def _write_plugin_with_tools(root: Path, name: str, tool_name: str) -> Path:
    """Plant a plugin with a single tool file using Agno's @tool decorator."""
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    (plugin_dir / "tools").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "tools" / f"{tool_name}.py").write_text(
        "from agno.tools import tool\n\n"
        "@tool()\n"
        f"def {tool_name}_fn() -> str:\n"
        f"    return 'from plugin {name}'\n",
        encoding="utf-8",
    )
    return plugin_dir


# ── Hooks ───────────────────────────────────────────────────────────


def test_apply_to_hooks_merges_plugin_event(tmp_path: Path) -> None:
    """A plugin's ``hooks/hooks.json`` block is parsed and its events
    end up in the shared hooks dict. The parsed entries use the same
    ``HookDefinition`` schema as settings.json hooks — no separate
    plugin type."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_hooks(
        user_ember,
        "alpha",
        {
            "PreToolUse": [{"type": "command", "command": "echo plugin-pre", "matcher": "Bash"}],
        },
    )

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    hooks: dict[str, list[HookDefinition]] = {}
    hook_loader = HookLoader(project_dir=tmp_path / "proj")
    loader.apply_to_hooks(hook_loader, hooks)

    assert "PreToolUse" in hooks
    assert len(hooks["PreToolUse"]) == 1
    assert hooks["PreToolUse"][0].command == "echo plugin-pre"
    assert hooks["PreToolUse"][0].matcher == "Bash"


def test_plugin_hooks_prepend_so_project_runs_last(tmp_path: Path) -> None:
    """Project hooks load into the dict first; plugin hooks prepend.
    Net order across the per-event list: plugins then project. The
    project still gets the last veto/transform — important so a user
    can override plugin behavior on a per-project basis without
    forking the plugin."""
    project_hook = HookDefinition(type="command", command="project-cmd")
    hooks: dict[str, list[HookDefinition]] = {"PostToolUse": [project_hook]}

    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_hooks(
        user_ember,
        "alpha",
        {
            "PostToolUse": [{"type": "command", "command": "plugin-cmd"}],
        },
    )

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")
    hook_loader = HookLoader(project_dir=tmp_path / "proj")
    loader.apply_to_hooks(hook_loader, hooks)

    commands = [h.command for h in hooks["PostToolUse"]]
    assert commands == ["plugin-cmd", "project-cmd"]


def test_apply_to_hooks_skips_disabled_plugin(tmp_path: Path) -> None:
    """Disabled plugins don't contribute hooks even though they're
    still discoverable via ``list_plugins``."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_hooks(
        user_ember,
        "alpha",
        {
            "PreToolUse": [{"type": "command", "command": "alpha-cmd"}],
        },
    )
    _write_plugin_with_hooks(
        user_ember,
        "beta",
        {
            "PreToolUse": [{"type": "command", "command": "beta-cmd"}],
        },
    )

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    hooks: dict[str, list[HookDefinition]] = {}
    hook_loader = HookLoader(project_dir=tmp_path / "proj")
    loader.apply_to_hooks(hook_loader, hooks, disabled={"alpha"})

    commands = [h.command for h in hooks["PreToolUse"]]
    assert commands == ["beta-cmd"]


def test_load_plugin_hooks_swallows_malformed_json(tmp_path: Path) -> None:
    """A broken ``hooks/hooks.json`` shouldn't take down the whole
    hooks pipeline. Log a warning, skip the plugin's hooks, continue."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    plugin_dir = user_ember / "broken"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "broken"}))
    (plugin_dir / "hooks").mkdir()
    (plugin_dir / "hooks" / "hooks.json").write_text("not-json")

    hook_loader = HookLoader(project_dir=tmp_path / "proj")
    hooks: dict[str, list[HookDefinition]] = {}
    # Should not raise.
    hook_loader.load_plugin_hooks(plugin_dir, hooks)
    assert hooks == {}


# ── MCP servers ─────────────────────────────────────────────────────


def test_apply_to_mcp_prefixes_server_names(tmp_path: Path) -> None:
    """Plugin-bundled MCP servers land in the configs dict with names
    prefixed ``<plugin>:<server>``. The raw name from the plugin's
    ``.mcp.json`` is never used directly."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_mcp(
        user_ember,
        "myplugin",
        {
            "mcpServers": {
                "fs": {"command": "/usr/bin/mcp-fs", "args": ["--root", "/tmp"]},
            },
        },
    )

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    servers: dict[str, MCPServerConfig] = {}
    mcp_loader = MCPConfigLoader(tmp_path / "proj")
    loader.apply_to_mcp(mcp_loader, servers)

    assert "myplugin:fs" in servers
    assert "fs" not in servers
    cfg = servers["myplugin:fs"]
    assert cfg.command == "/usr/bin/mcp-fs"
    assert cfg.args == ["--root", "/tmp"]


def test_apply_to_mcp_first_wins_on_collision(tmp_path: Path) -> None:
    """If two plugins were to somehow register the same prefixed key,
    the first wins. (In practice this is unreachable since plugin
    names are unique — but the policy makes the collision-resolution
    rule explicit.)"""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_mcp(user_ember, "a", {"mcpServers": {"shared": {"command": "/a"}}})

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    servers: dict[str, MCPServerConfig] = {
        "a:shared": MCPServerConfig(name="a:shared", command="/preexisting"),
    }
    mcp_loader = MCPConfigLoader(tmp_path / "proj")
    loader.apply_to_mcp(mcp_loader, servers)

    # First-wins: the pre-existing entry is preserved.
    assert servers["a:shared"].command == "/preexisting"


def test_apply_to_mcp_supports_fallback_filename(tmp_path: Path) -> None:
    """Claude Code's spec uses ``.mcp.json``; tolerate ``mcp.json``
    (no leading dot) too for plugin authors who forget the convention."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_mcp(
        user_ember,
        "p",
        {"mcpServers": {"x": {"command": "/x"}}},
        filename="mcp.json",
    )

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    servers: dict[str, MCPServerConfig] = {}
    loader.apply_to_mcp(MCPConfigLoader(tmp_path / "proj"), servers)
    assert "p:x" in servers


def test_apply_to_mcp_skips_disabled(tmp_path: Path) -> None:
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_mcp(user_ember, "off", {"mcpServers": {"x": {"command": "/x"}}})
    _write_plugin_with_mcp(user_ember, "on", {"mcpServers": {"y": {"command": "/y"}}})

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    servers: dict[str, MCPServerConfig] = {}
    loader.apply_to_mcp(MCPConfigLoader(tmp_path / "proj"), servers, disabled={"off"})
    assert "on:y" in servers
    assert "off:x" not in servers


# ── Custom tools ────────────────────────────────────────────────────


def test_collect_tool_dirs_returns_only_plugins_with_tools(tmp_path: Path) -> None:
    """``collect_tool_dirs`` returns ``(plugin_name, tools_dir)`` for
    enabled plugins that bundle a ``tools/`` directory. Plugins
    without tools are filtered out so the consumer (custom_loader)
    doesn't have to re-stat."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_tools(user_ember, "withtools", "demo")
    # A plugin with hooks but no tools should not appear.
    _write_plugin_with_hooks(user_ember, "withouttools", {})

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    dirs = loader.collect_tool_dirs()
    names = [name for name, _ in dirs]
    assert "withtools" in names
    assert "withouttools" not in names


def test_collect_tool_dirs_skips_disabled(tmp_path: Path) -> None:
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_tools(user_ember, "off", "demo")
    _write_plugin_with_tools(user_ember, "on", "demo")

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    dirs = loader.collect_tool_dirs(disabled={"off"})
    names = [name for name, _ in dirs]
    assert names == ["on"]


def test_apply_to_agents_namespaces_loaded_agents(tmp_path: Path) -> None:
    """Symmetric to ``test_apply_to_skills_namespaces_loaded_skills``
    but for agents. A plugin named ``foo`` whose ``agents/demo.md``
    carries ``name: demo`` lands in the AgentPool as ``foo:demo``."""
    from ember_code.core.config.settings import Settings
    from ember_code.core.plugins.loader import PluginLoader
    from ember_code.core.pool import AgentPool

    user_ember = tmp_path / "home" / ".ember" / "plugins"
    plugin_dir = user_ember / "foo"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "foo"}),
        encoding="utf-8",
    )
    (plugin_dir / "agents").mkdir(parents=True)
    (plugin_dir / "agents" / "demo.md").write_text(
        "---\nname: demo\ndescription: A bundled agent\ntools: [Read]\n---\n\nSystem prompt.\n",
        encoding="utf-8",
    )

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    pool = AgentPool()
    pool.load_definitions(Settings(), project_dir=tmp_path / "proj")
    loader.apply_to_agents(pool)

    names = {d.name for d in pool.list_agents()}
    assert "foo:demo" in names
    assert "demo" not in names


def test_apply_to_agents_honors_disabled(tmp_path: Path) -> None:
    """Symmetric to the skills-disabled test: a disabled plugin's
    agents never make it into the AgentPool, but the plugin itself
    is still discoverable for the panel."""
    from ember_code.core.config.settings import Settings
    from ember_code.core.plugins.loader import PluginLoader
    from ember_code.core.pool import AgentPool

    user_ember = tmp_path / "home" / ".ember" / "plugins"
    for plugin_name in ("off", "on"):
        plugin_dir = user_ember / plugin_name
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": plugin_name})
        )
        (plugin_dir / "agents").mkdir(parents=True)
        (plugin_dir / "agents" / "demo.md").write_text(
            "---\nname: demo\ndescription: x\ntools: [Read]\n---\nBody.\n",
        )

    loader = PluginLoader()
    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader.load_all(project_dir=tmp_path / "proj")

    pool = AgentPool()
    pool.load_definitions(Settings(), project_dir=tmp_path / "proj")
    loader.apply_to_agents(pool, disabled={"off"})

    names = {d.name for d in pool.list_agents()}
    assert "on:demo" in names
    assert "off:demo" not in names
    assert loader.get("off") is not None  # still discoverable


def test_load_custom_tools_namespaces_plugin_toolkits(tmp_path: Path) -> None:
    """End-to-end: a plugin's ``tools/<file>.py`` becomes a toolkit named
    ``custom_<plugin>_<file>``. The prefix prevents collisions with
    same-named user files in ``~/.ember/tools/``."""
    from ember_code.core.tools.custom_loader import load_custom_tools

    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin_with_tools(user_ember, "mp", "demo")

    with patch.object(Path, "home", return_value=tmp_path / "home"):
        loader = PluginLoader()
        loader.load_all(project_dir=tmp_path / "proj")
        plugin_dirs = loader.collect_tool_dirs()
        toolkits = load_custom_tools(
            project_dir=tmp_path / "proj",
            plugin_tool_dirs=plugin_dirs,
        )

    names = [t.name for t in toolkits]
    assert "custom_mp_demo" in names
