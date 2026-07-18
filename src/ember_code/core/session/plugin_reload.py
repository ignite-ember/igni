"""Hot-reload orchestration for a live :class:`Session`.

Extracted from :mod:`ember_code.core.session.core` — the two
methods (``reload_plugins`` + ``_reapply_plugin_mcp_configs``)
that hot-swap plugin contents from disk into the running
session's four wiring points (plugins + skills + agents +
hooks + MCP configs).

Constructor takes a closure over Session's own
``_init_plugins_output_styles_hooks`` /
``_init_agent_and_skill_pools`` / ``_build_main_agent`` /
``_disabled_plugins`` accessors — the coordinator owns the
orchestration flow; Session still owns the sub-system re-inits
because they touch a wide slice of Session state.

Returns :class:`PluginReloadCounts` (schema-typed — no raw
dicts on the boundary).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.config import MCPConfigLoader
from ember_code.core.plugins import PluginLoader
from ember_code.core.session.schemas import PluginReloadCounts

logger = logging.getLogger(__name__)


class PluginReloadOrchestrator:
    """Coordinator that hot-reloads plugin contributions into a
    live session.

    Life-cycle: one instance per Session (composed in
    ``Session.__init__``); :meth:`reload` may be called any
    number of times.
    """

    def __init__(
        self,
        *,
        project_dir: Path,
        mcp_manager: MCPClientManager,
        plugin_loader_ref: Callable[[], PluginLoader],
        disabled_plugins_ref: Callable[[], set[str]],
        rebuild_plugins_and_hooks: Callable[[], None],
        rebuild_agent_and_skill_pools: Callable[[], None],
        rebuild_main_team: Callable[[], None],
        skill_pool_ref: Callable[[], object],
        agent_pool_ref: Callable[[], object],
        hooks_map_ref: Callable[[], dict],
        disconnect_removed_mcps: Callable[[set[str]], object],
        auto_connect_mcps: Callable[[set[str]], object],
    ) -> None:
        self._project_dir = project_dir
        self._mcp_manager = mcp_manager
        self._plugin_loader_ref = plugin_loader_ref
        self._disabled_plugins_ref = disabled_plugins_ref
        self._rebuild_plugins_and_hooks = rebuild_plugins_and_hooks
        self._rebuild_agent_and_skill_pools = rebuild_agent_and_skill_pools
        self._rebuild_main_team = rebuild_main_team
        self._skill_pool_ref = skill_pool_ref
        self._agent_pool_ref = agent_pool_ref
        self._hooks_map_ref = hooks_map_ref
        self._disconnect_removed_mcps = disconnect_removed_mcps
        self._auto_connect_mcps = auto_connect_mcps

    def reload(self) -> PluginReloadCounts:
        """Hot-reload plugin contents from disk — no session restart.

        Re-scans every plugin root and re-applies each enabled
        plugin's bundled contents to the four wiring points:

        * **Hooks** — rebuilt via ``_hook_loader`` then merged.
        * **Skills** — fresh :class:`SkillPool` reload from disk.
        * **Agents** — fresh :class:`AgentPool` rebuilt; the main
          team is rebuilt at the end so the new agents are
          attached.
        * **MCP server configs** — merged into
          ``mcp_manager.configs``. Connections aren't auto-started
          for the new set here — :meth:`_reapply_mcp` fires them
          in the background.

        Returns a :class:`PluginReloadCounts` for the caller's chat
        confirmation.
        """
        # Full plugin / output-style / hooks / pool re-init —
        # matches the constructor's ordering exactly so a hot-
        # reload produces the same end-state as a fresh session
        # boot. Refreshes ``output_styles`` too.
        self._rebuild_plugins_and_hooks()
        self._rebuild_agent_and_skill_pools()

        self._reapply_mcp()

        # Rebuild the main team so newly-bundled custom tools
        # (``<plugin>/tools/*.py``) and the refreshed agent pool are
        # visible to the live agent.
        self._rebuild_main_team()

        plugin_loader = self._plugin_loader_ref()
        skill_pool = self._skill_pool_ref()
        agent_pool = self._agent_pool_ref()
        hooks_map = self._hooks_map_ref()
        return PluginReloadCounts(
            plugins=len(plugin_loader.list_plugins()),
            skills=len(skill_pool.list_skills()),
            agents=len(agent_pool.list_agents()),
            hooks=sum(len(hl) for hl in hooks_map.values()),
        )

    def _reapply_mcp(self) -> None:
        """Sync ``mcp_manager.configs`` with the current enabled-plugin
        set, disconnecting removed servers + auto-connecting added
        ones in the background.

        MCP is symmetric in both directions. Enabling a plugin
        wires its servers in + auto-connects them; disabling a
        plugin wires them OUT + disconnects them. Without the
        disable side, a user who turns off a plugin sees its
        skills/agents/hooks disappear but the MCP server keeps
        running and showing up in ``/mcp`` — confusing state.
        """
        plugin_loader = self._plugin_loader_ref()
        disabled = self._disabled_plugins_ref()
        plugin_name_prefixes = tuple(f"{p.name}:" for p in plugin_loader.list_plugins())
        previously_plugin_owned = {
            name
            for name in self._mcp_manager.configs
            if any(name.startswith(p) for p in plugin_name_prefixes)
        }
        for name in previously_plugin_owned:
            self._mcp_manager.configs.pop(name, None)
        plugin_loader.apply_to_mcp(
            MCPConfigLoader(self._project_dir),
            self._mcp_manager.configs,
            disabled=disabled,
        )
        now_plugin_owned = {
            name
            for name in self._mcp_manager.configs
            if any(name.startswith(p) for p in plugin_name_prefixes)
        }
        added_mcp_names = now_plugin_owned - previously_plugin_owned
        removed_mcp_names = previously_plugin_owned - now_plugin_owned

        if removed_mcp_names:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._disconnect_removed_mcps(removed_mcp_names))
            except RuntimeError:
                logger.debug(
                    "No running loop — skipping MCP disconnect for: %s",
                    sorted(removed_mcp_names),
                )

        if added_mcp_names:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._auto_connect_mcps(added_mcp_names))
            except RuntimeError:
                logger.debug(
                    "Skipping MCP auto-connect (no running loop); use /mcp connect to start: %s",
                    sorted(added_mcp_names),
                )
