"""Plugin support for Claude-Code-compatible plugins.

A plugin is a directory containing ``.claude-plugin/plugin.json`` plus
optional ``skills/``, ``agents/``, ``hooks/hooks.json``, ``.mcp.json``,
and ``tools/`` subdirectories. Plugins are discovered from four roots
(both ``.claude/`` and ``.ember/``, both user-global and project-local)
and their bundled contents are namespaced with ``<plugin>:`` to avoid
collisions across plugins.

The package owns discovery (:class:`PluginLoader`), persisted state
(disabled list, install pins — see :mod:`state`), and the marketplace
registry (Phase 2). The actual loading of skills/agents/hooks/MCP/tools
goes through the existing per-type loaders — this module just hands
them the right directories with a namespace prefix.
"""

from ember_code.core.plugins.git import GitClient, GitError
from ember_code.core.plugins.installer import PluginError, PluginInstaller
from ember_code.core.plugins.loader import PluginLoader
from ember_code.core.plugins.marketplaces import (
    MarketplaceCatalog,
    MarketplaceEntry,
    MarketplacePluginEntry,
    MarketplaceRegistry,
    add_marketplace,
    fetch_catalog,
    load_registry,
    refresh_marketplace,
    remove_marketplace,
    resolve_install_ref,
    save_registry,
)
from ember_code.core.plugins.models import (
    MarketplaceInfo,
    MarketplacePluginInfo,
    PluginDefinition,
    PluginInfo,
    PluginManifest,
    PluginSource,
)
from ember_code.core.plugins.state import (
    PluginsState,
    load_state,
    save_state,
)

__all__ = [
    "GitClient",
    "GitError",
    "PluginError",
    "PluginInstaller",
    "PluginLoader",
    "PluginManifest",
    "PluginDefinition",
    "PluginInfo",
    "PluginSource",
    "MarketplaceInfo",
    "MarketplacePluginInfo",
    "PluginsState",
    "MarketplaceCatalog",
    "MarketplaceEntry",
    "MarketplacePluginEntry",
    "MarketplaceRegistry",
    "load_state",
    "save_state",
    "load_registry",
    "save_registry",
    "fetch_catalog",
    "add_marketplace",
    "remove_marketplace",
    "refresh_marketplace",
    "resolve_install_ref",
]
