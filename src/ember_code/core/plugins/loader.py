"""Plugin discovery + namespace-prefixed apply to existing loaders.

Scans four roots in priority order (later wins same-name collisions):

    1. ~/.claude/plugins/           (Claude user-global)
    2. ~/.ember/plugins/            (ember user-global)
    3. <project>/.claude/plugins/   (Claude project-local)
    4. <project>/.ember/plugins/    (ember project-local)

A plugin = directory containing ``.claude-plugin/plugin.json``. Anything
else under the roots is ignored silently — leaves room for stray files,
notes, gitkeeps, etc.

This module does NOT itself load skills/agents/hooks/MCP — it hands the
right subdirectories to the existing per-type loaders with a namespace
argument so contents land as ``<plugin>:<name>`` and can't collide.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.plugins.models import (
    PluginDefinition,
    PluginManifest,
    PluginSource,
)

if TYPE_CHECKING:
    from ember_code.core.hooks.loader import HookLoader
    from ember_code.core.hooks.schemas import HookDefinition
    from ember_code.core.mcp.config import MCPConfigLoader, MCPServerConfig
    from ember_code.core.pool import AgentPool
    from ember_code.core.skills.loader import SkillPool

logger = logging.getLogger(__name__)


class PluginLoader:
    """Discovers plugins and applies their bundled extensions."""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginDefinition] = {}

    # ── Discovery ────────────────────────────────────────────────────

    def load_all(self, project_dir: Path | None = None) -> None:
        """Scan all four roots and populate the plugin registry.

        Same-name collisions across roots resolve by ``priority``: the
        higher-priority root wins. Plugins disabled via state are still
        recorded here — the apply steps are where ``disabled`` is
        honored, so the panel can still show disabled plugins.
        """
        if project_dir is None:
            project_dir = Path.cwd()

        roots: list[tuple[str, Path, int]] = [
            ("user-claude", Path.home() / ".claude" / "plugins", 1),
            ("user-ember", Path.home() / ".ember" / "plugins", 2),
            ("project-claude", project_dir / ".claude" / "plugins", 3),
            ("project-ember", project_dir / ".ember" / "plugins", 4),
        ]

        for root_kind, root_path, priority in roots:
            self._load_root(root_kind, root_path, priority)

    def _load_root(self, root_kind: str, root: Path, priority: int) -> None:
        if not root.is_dir():
            return

        for plugin_dir in sorted(root.iterdir()):
            if not plugin_dir.is_dir():
                continue

            manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
            if not manifest_path.is_file():
                # Not a Claude-Code-shaped plugin; skip silently.
                continue

            try:
                manifest = PluginManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
            except Exception as e:
                logger.warning("Failed to parse plugin manifest at %s: %s", manifest_path, e)
                continue

            definition = PluginDefinition(
                manifest=manifest,
                source=PluginSource(
                    root=root_kind,  # type: ignore[arg-type]
                    path=plugin_dir,
                    priority=priority,
                ),
                has_skills=(plugin_dir / "skills").is_dir(),
                has_agents=(plugin_dir / "agents").is_dir(),
                has_hooks=(plugin_dir / "hooks" / "hooks.json").is_file(),
                has_mcp=(plugin_dir / ".mcp.json").is_file() or (plugin_dir / "mcp.json").is_file(),
                has_tools=(plugin_dir / "tools").is_dir(),
            )

            existing = self._plugins.get(manifest.name)
            if existing is None or priority > existing.source.priority:
                self._plugins[manifest.name] = definition

    # ── Inspection ───────────────────────────────────────────────────

    def list_plugins(self) -> list[PluginDefinition]:
        """All discovered plugins, sorted by name. Includes disabled."""
        return sorted(self._plugins.values(), key=lambda p: p.name)

    def get(self, name: str) -> PluginDefinition | None:
        return self._plugins.get(name)

    # ── Apply: hand bundled subdirs to existing loaders ─────────────

    def apply_to_skills(
        self,
        skill_pool: SkillPool,
        *,
        disabled: set[str] | None = None,
    ) -> None:
        """Load each enabled plugin's ``skills/`` into the SkillPool.

        Uses the plugin's name as a namespace prefix so loaded skills
        are addressable as ``<plugin>:<skill>``. Priority is the
        plugin's source-root priority.
        """
        disabled = disabled or set()
        for plugin in self._plugins.values():
            if plugin.name in disabled or not plugin.has_skills:
                continue
            skill_pool.load_directory(
                plugin.root_path / "skills",
                priority=plugin.source.priority,
                namespace=plugin.name,
            )

    def apply_to_agents(
        self,
        agent_pool: AgentPool,
        *,
        disabled: set[str] | None = None,
    ) -> None:
        """Load each enabled plugin's ``agents/`` into the AgentPool.

        AgentPool exposes ``_load_directory`` (single-underscore — used
        across the package, not strictly private). The namespacing
        rule mirrors skills: ``<plugin>:<agent>``.
        """
        disabled = disabled or set()
        for plugin in self._plugins.values():
            if plugin.name in disabled or not plugin.has_agents:
                continue
            agent_pool._load_directory(
                plugin.root_path / "agents",
                priority=plugin.source.priority,
                namespace=plugin.name,
            )

    def apply_to_hooks(
        self,
        hook_loader: HookLoader,
        hooks: dict[str, list[HookDefinition]],
        *,
        disabled: set[str] | None = None,
    ) -> None:
        """Merge each enabled plugin's ``hooks/hooks.json`` into *hooks*.

        Plugins are *prepended* to each event's bucket so project-level
        hooks (which were loaded last by ``HookLoader.load()``) still
        run after plugin hooks — giving the project's veto/transform
        the final word in any chain.
        """
        disabled = disabled or set()
        for plugin in self._plugins.values():
            if plugin.name in disabled or not plugin.has_hooks:
                continue
            hook_loader.load_plugin_hooks(plugin.root_path, hooks)

    def apply_to_mcp(
        self,
        mcp_config_loader: MCPConfigLoader,
        servers: dict[str, MCPServerConfig],
        *,
        disabled: set[str] | None = None,
    ) -> None:
        """Merge each enabled plugin's ``.mcp.json`` into *servers*.

        Servers are registered with names prefixed ``<plugin>:<server>``.
        First-wins on collision — see ``MCPConfigLoader.load_plugin_servers``.
        """
        disabled = disabled or set()
        for plugin in self._plugins.values():
            if plugin.name in disabled or not plugin.has_mcp:
                continue
            mcp_config_loader.load_plugin_servers(plugin.root_path, plugin.name, servers)

    def collect_tool_dirs(
        self,
        *,
        disabled: set[str] | None = None,
    ) -> list[tuple[str, Path]]:
        """Return ``(plugin_name, tools_dir)`` for every enabled plugin
        that bundles a ``tools/`` directory.

        Used by ``load_custom_tools(plugin_tool_dirs=...)``. The
        returned list preserves plugin discovery order so toolkit
        registration is deterministic across sessions.
        """
        disabled = disabled or set()
        return [
            (p.name, p.root_path / "tools")
            for p in self._plugins.values()
            if p.name not in disabled and p.has_tools
        ]
