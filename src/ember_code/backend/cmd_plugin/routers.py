"""Router classes — dispatch subcommand tokens to :class:`PluginVerb` instances.

Three routers, one per command family:

* :class:`PluginInstallRouter` — install / update / remove
* :class:`MarketplaceRouter` — add / list / remove / refresh
* :class:`PluginsToggleRouter` — enable / disable + bare-``/plugins`` panel

Each holds a ``{verb.name: verb_instance}`` dict built from a
tuple of verb classes at construction — dict-comprehension over
the class list, not string names. This is the Pattern-4 replacement
for the god-coordinator's ``self._PLUGIN_ACTIONS: dict[str, str]``
method-name dispatch tables. Adding a new verb means adding a class
to the tuple; no dict edit, no method-name string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.cmd_plugin.verbs import (
    InstallVerb,
    MarketplaceAddVerb,
    MarketplaceListVerb,
    MarketplaceRefreshVerb,
    MarketplaceRemoveVerb,
    PluginsDisableVerb,
    PluginsEnableVerb,
    PluginVerb,
    RemoveVerb,
    UpdateVerb,
)
from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.cmd_plugin.context import SlashCommandContext


class _VerbRouter:
    """Base for the three routers. Owns the ``{name: verb}`` dict
    (built once at construction from :attr:`_VERBS`) and the
    dispatch method. Subclasses override :attr:`_VERBS` and
    :attr:`_UNKNOWN_MSG`."""

    _VERBS: tuple[type[PluginVerb], ...] = ()
    _EMPTY_MSG: str = ""
    _UNKNOWN_MSG: str = ""

    def __init__(self) -> None:
        # Dict of live verb INSTANCES keyed on their own ``name`` —
        # not method-name strings. Verifies at construction that
        # every verb class advertises its ``name``.
        self._verbs: dict[str, PluginVerb] = {v.name: v() for v in self._VERBS}

    def _unknown(self, token: str) -> CommandResult:
        return CommandResult.error(self._UNKNOWN_MSG.format(token=token))

    async def dispatch(self, ctx: SlashCommandContext, rest: list[str]) -> CommandResult:
        if not rest:
            return CommandResult.error(self._EMPTY_MSG)
        token = rest[0].lower()
        verb = self._verbs.get(token)
        if verb is None:
            return self._unknown(token)
        return await verb.run(ctx, rest[1:])


class PluginInstallRouter(_VerbRouter):
    """Dispatches ``/plugin <install|update|remove> …``."""

    _VERBS = (InstallVerb, UpdateVerb, RemoveVerb)
    _EMPTY_MSG = (
        "Usage: /plugin install <git-url|@marketplace/plugin> | "
        "/plugin update <name> | /plugin remove <name> | "
        "/plugin marketplace add|list|remove|refresh"
    )
    _UNKNOWN_MSG = (
        "Unknown /plugin subcommand: '{token}'. Use install / update / remove / marketplace."
    )


class MarketplaceRouter(_VerbRouter):
    """Dispatches ``/plugin marketplace <add|list|remove|refresh> …``."""

    _VERBS = (
        MarketplaceAddVerb,
        MarketplaceListVerb,
        MarketplaceRemoveVerb,
        MarketplaceRefreshVerb,
    )
    _EMPTY_MSG = "Usage: /plugin marketplace add <url> | list | remove <name> | refresh [<name>]"
    _UNKNOWN_MSG = (
        "Unknown /plugin marketplace action: '{token}'. Use add / list / remove / refresh."
    )


class PluginsToggleRouter(_VerbRouter):
    """Dispatches ``/plugins [<enable|disable> <name>]``. Bare
    ``/plugins`` (no subcommand) returns the panel-open action —
    handled inline in :meth:`dispatch` since it's not a
    :class:`PluginVerb` (no gateway call, no arg parse)."""

    _VERBS = (PluginsEnableVerb, PluginsDisableVerb)
    _UNKNOWN_MSG = (
        "Unknown /plugins subcommand: '{token}'. Use `enable` or "
        "`disable`, or run `/plugins` alone to open the panel."
    )

    async def dispatch(self, ctx: SlashCommandContext, rest: list[str]) -> CommandResult:
        # Bare ``/plugins`` — open the TUI panel. Not a verb because
        # there's no arg parsing, gateway call, or Result shape.
        if not rest:
            return CommandResult.for_action(CommandAction.PLUGINS)
        return await super().dispatch(ctx, rest)


__all__ = [
    "PluginInstallRouter",
    "MarketplaceRouter",
    "PluginsToggleRouter",
]
