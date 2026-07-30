"""Three :class:`SlashCommand` instances — the top of the command tree.

Replaces the three deleted module-level ``async def cmd_*`` shims:

* :class:`PluginCommand` — ``/plugin`` (install / update / remove /
  marketplace family).
* :class:`PluginMarketplaceCommand` — legacy sub-tree entrypoint
  ``/plugin marketplace …`` retained so any external caller
  threading through the direct symbol path keeps working. The
  built-in registry only wires :class:`PluginCommand` — this class
  is a public shim only.
* :class:`PluginsCommand` — ``/plugins`` (open panel + enable /
  disable toggles).

Each ``run(handler, args)`` implementation:

1. Reads ``session.plugin_data_dir`` (the new named seam) fresh.
2. Constructs a :class:`PluginBackendGateway` — per-command, not
   per-SlashCommand-instance. Matches the pre-refactor
   ``PluginSlashCommand(handler)`` shape (fresh state every call).
3. Builds a :class:`SlashCommandContext` bundling session +
   data_dir + gateway.
4. Tokenises ``args`` and delegates to the appropriate router.

Docstrings from the deleted ``cmd_plugin`` / ``cmd_plugin_marketplace``
/ ``cmd_plugins`` shims (usage tables, forms lists) live on these
classes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.cmd_plugin.context import SlashCommandContext
from ember_code.backend.cmd_plugin.gateway import PluginBackendGateway
from ember_code.backend.cmd_plugin.routers import (
    MarketplaceRouter,
    PluginInstallRouter,
    PluginsToggleRouter,
)
from ember_code.backend.command_result import CommandResult

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler


class _BaseSlashCommand:
    """Small shared helper: builds a fresh
    :class:`SlashCommandContext` from a :class:`CommandHandler`. All
    three command classes need this; extracting keeps
    :meth:`PluginCommand.run` etc. focused on tokenisation +
    dispatch."""

    def _make_context(self, handler: CommandHandler) -> SlashCommandContext:
        session = handler.session
        # ``plugin_data_dir`` is the new public property on Session —
        # avoids the previous ``session.settings.storage.data_dir``
        # three-level Demeter chain.
        data_dir = session.plugin_data_dir
        gateway = PluginBackendGateway(data_dir=data_dir)
        return SlashCommandContext(session=session, plugin_data_dir=data_dir, gateway=gateway)


class PluginCommand(_BaseSlashCommand):
    """``/plugin`` — install, update, remove plugins; manage marketplaces.

    Forms:
      /plugin install <git-url>                 install directly from URL
      /plugin install @<marketplace>/<plugin>   install via marketplace catalog
      /plugin install <ref> --ref <branch|tag|sha>   pin at install time
      /plugin update <name>                     fetch + reset to latest HEAD
      /plugin update <name> --ref <ref>         retarget to branch/tag/SHA
      /plugin remove <name>                     uninstall (deletes plugin dir)
      /plugin marketplace add <git-url>         register a marketplace
      /plugin marketplace list                  show registered marketplaces
      /plugin marketplace remove <name>         unregister (plugins kept)
      /plugin marketplace refresh [<name>]      re-fetch one or all catalogs

    Most actions require ``git`` on PATH.
    """

    name: str = "/plugin"
    description: str = "Install / update / remove a plugin"

    def __init__(self) -> None:
        # Per-command routers — cheap to construct (they own only
        # a small dict of verb instances) and stateless across
        # command invocations, so constructing once per SlashCommand
        # instance is fine.
        self._install_router = PluginInstallRouter()
        self._marketplace_router = MarketplaceRouter()

    async def run(self, handler: CommandHandler, args: str) -> CommandResult:
        parts = args.strip().split()
        if not parts:
            return CommandResult.error(
                "Usage: /plugin install <git-url|@marketplace/plugin> | "
                "/plugin update <name> | /plugin remove <name> | "
                "/plugin marketplace add|list|remove|refresh"
            )
        subcommand = parts[0].lower()
        ctx = self._make_context(handler)
        # Marketplace family gets its own router (add/list/remove/refresh);
        # every other subcommand goes to the install router.
        if subcommand == "marketplace":
            return await self._marketplace_router.dispatch(ctx, parts[1:])
        return await self._install_router.dispatch(ctx, parts)


class PluginMarketplaceCommand(_BaseSlashCommand):
    """Legacy sub-tree entrypoint kept as a stable class name for any
    external caller (or test) that reaches for ``PluginMarketplaceCommand``
    directly. Not registered in :class:`BuiltinCommandRegistry` — the
    ``/plugin marketplace …`` subtree flows through
    :class:`PluginCommand` — this is a shim only."""

    name: str = "/plugin marketplace"
    description: str = "Manage plugin marketplaces"

    def __init__(self) -> None:
        self._router = MarketplaceRouter()

    async def run(self, handler: CommandHandler, args: str) -> CommandResult:
        rest = args.strip().split()
        ctx = self._make_context(handler)
        return await self._router.dispatch(ctx, rest)


class PluginsCommand(_BaseSlashCommand):
    """``/plugins`` — open the plugins panel, or toggle enable/disable directly.

    Forms:
      /plugins                    — open the plugins TUI panel
      /plugins enable <name>      — enable a disabled plugin (no panel)
      /plugins disable <name>     — disable an enabled plugin (no panel)

    Enable/disable persist to ``~/.ember/plugins.json`` and take
    effect on the next session start (and hot-reload for the current
    one).
    """

    name: str = "/plugins"
    description: str = "Open the plugins panel"

    def __init__(self) -> None:
        self._router = PluginsToggleRouter()

    async def run(self, handler: CommandHandler, args: str) -> CommandResult:
        # ``/plugins enable <name>`` — split into two tokens
        # (subcommand + name). The toggle-verb reads the raw name
        # as ``rest[0]`` so anything with internal whitespace flows
        # through intact (plugin names shouldn't have spaces, but
        # the pre-refactor code used ``split(None, 1)`` — preserve).
        parts = args.strip().split(None, 1)
        rest: list[str]
        if not parts:
            rest = []
        else:
            subcommand = parts[0]
            name_raw = parts[1].strip() if len(parts) > 1 else ""
            rest = [subcommand, name_raw]
        ctx = self._make_context(handler)
        return await self._router.dispatch(ctx, rest)


__all__ = ["PluginCommand", "PluginMarketplaceCommand", "PluginsCommand"]
