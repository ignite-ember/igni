"""Public surface of the ``cmd_plugin`` package.

Exports:

* :data:`PLUGIN_COMMANDS` — tuple of :class:`SlashCommand`
  instances the registry consumes.
* :func:`cmd_plugin`, :func:`cmd_plugin_marketplace`,
  :func:`cmd_plugins` — bare-async-callable shims delegating to the
  SlashCommand instances so the existing
  :class:`BuiltinCommandRegistry` (which still uses the
  ``(handler, args) -> CommandResult`` callable shape for every
  built-in) keeps wiring the plugin family unchanged.
* Class re-exports so tests and downstream code can construct or
  patch the coordinators directly.

The package layout intentionally mirrors the design:

* ``context.py`` — :class:`SlashCommandContext` value bag.
* ``gateway.py`` — :class:`PluginBackendGateway` (the single seam
  for installer/marketplace calls).
* ``verbs.py`` — the :class:`PluginVerb` hierarchy.
* ``routers.py`` — three router classes (install / marketplace /
  toggle).
* ``bulk_refresh.py`` — :class:`BulkRefreshRunner` for
  ``/plugin marketplace refresh`` (all).
* ``commands.py`` — the three top-level :class:`SlashCommand`
  classes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.cmd_plugin.commands import (
    PluginCommand,
    PluginMarketplaceCommand,
    PluginsCommand,
)
from ember_code.backend.cmd_plugin.context import SlashCommandContext
from ember_code.backend.cmd_plugin.gateway import PluginBackendGateway

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.backend.command_result import CommandResult


# ── SlashCommand instances (the new OOP surface) ───────────────────

# Module-level singletons — construction is cheap (a couple of dicts
# of verb instances) and each command is stateless across
# invocations because per-command mutable state lives on the
# per-call :class:`SlashCommandContext` / :class:`PluginBackendGateway`.
_PLUGIN_COMMAND = PluginCommand()
_PLUGIN_MARKETPLACE_COMMAND = PluginMarketplaceCommand()
_PLUGINS_COMMAND = PluginsCommand()

PLUGIN_COMMANDS: tuple = (
    _PLUGIN_COMMAND,
    _PLUGINS_COMMAND,
)


# ── Legacy bare-async-callable shims ───────────────────────────────
#
# The :class:`BuiltinCommandRegistry` currently imports these three
# names at module top and stores them in its dispatch table. Rather
# than teach the registry two entry shapes in this PR, the shims
# stay — each is two lines and delegates straight to the SlashCommand
# instance. When every cmd_*.py migrates to the SlashCommand
# protocol the registry can drop the bare-callable path and these
# shims disappear.


async def cmd_plugin(handler: CommandHandler, args: str) -> CommandResult:
    """``/plugin`` — install / update / remove / marketplace."""
    return await _PLUGIN_COMMAND.run(handler, args)


async def cmd_plugin_marketplace(
    handler: CommandHandler,
    rest: list[str],
    _data_dir: str | None = None,
) -> CommandResult:
    """Legacy entrypoint kept for any external caller reaching for
    the marketplace sub-tree directly. ``_data_dir`` is preserved
    only for backward-compat with the pre-refactor signature — the
    gateway now reads ``data_dir`` fresh from the session's new
    :attr:`Session.plugin_data_dir` property."""
    _ = _data_dir  # legacy parameter, unused
    return await _PLUGIN_MARKETPLACE_COMMAND.run(handler, " ".join(rest))


async def cmd_plugins(handler: CommandHandler, args: str) -> CommandResult:
    """``/plugins`` — open the panel or toggle enable/disable."""
    return await _PLUGINS_COMMAND.run(handler, args)


__all__ = [
    "PLUGIN_COMMANDS",
    "PluginBackendGateway",
    "PluginCommand",
    "PluginMarketplaceCommand",
    "PluginsCommand",
    "SlashCommandContext",
    "cmd_plugin",
    "cmd_plugin_marketplace",
    "cmd_plugins",
]
