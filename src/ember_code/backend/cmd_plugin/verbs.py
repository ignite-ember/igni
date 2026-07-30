"""Polymorphic verb hierarchy for the ``/plugin`` and ``/plugins`` families.

Replaces the god-coordinator's three ``{"install": "_install", ...}``
string-to-method-name dispatch dicts with proper polymorphism: each
verb is a concrete class inheriting :class:`PluginVerb` (or one of
its arg-shape specialisations), overriding ``_execute``. The router
holds ``{verb.name: verb_instance}`` dicts of live instances.

Every verb:

* Declares ``name: ClassVar[str]`` — its subcommand token.
* Declares ``_args_cls: ClassVar[type[BaseModel]]`` — the Pydantic
  arg schema whose ``.parse(rest)`` returns either the validated
  args or an :class:`ArgsParseError` (the base's ``run`` matches on
  the return type — subclasses always receive a validated model).
* Implements ``async def _execute(ctx, args)`` — the actual verb
  body, which is now try/except-free because the gateway returns
  Result models.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from ember_code.backend.cmd_plugin.bulk_refresh import BulkRefreshRunner
from ember_code.backend.command_result import CommandResult
from ember_code.backend.plugin_schemas import (
    ArgsParseError,
    InstallArgs,
    MarketplaceAddArgs,
    MarketplaceRefreshArgs,
    MarketplaceRemoveArgs,
    PluginsToggleArgs,
    RemoveArgs,
    UpdateArgs,
)
from ember_code.core.plugins.state import save_state

if TYPE_CHECKING:
    from ember_code.backend.cmd_plugin.context import SlashCommandContext


class PluginVerb(ABC):
    """Base class for every ``/plugin ...`` subcommand.

    Subclasses override :attr:`_args_cls` + :meth:`_execute`. The
    base's :meth:`run` handles the parse + error-branch centrally so
    subclasses always receive a validated args model.
    """

    name: ClassVar[str]
    _args_cls: ClassVar[type]  # Pydantic model with ``.parse(rest)``

    async def run(self, ctx: SlashCommandContext, rest: list[str]) -> CommandResult:
        """Parse ``rest`` via the subclass's args schema, then
        delegate to :meth:`_execute` with the validated model."""
        parsed = self._args_cls.parse(rest)
        if isinstance(parsed, ArgsParseError):
            return CommandResult.error(parsed.message)
        return await self._execute(ctx, parsed)

    @abstractmethod
    async def _execute(self, ctx: SlashCommandContext, args) -> CommandResult:
        """Verb-specific logic. Receives validated args and the
        shared :class:`SlashCommandContext`. Free of try/except —
        the gateway owns exception handling."""


# ── /plugin install / update / remove ──────────────────────────────


class InstallVerb(PluginVerb):
    """Handles ``/plugin install <git-url|@marketplace/plugin> [--ref <ref>]``."""

    name: ClassVar[str] = "install"
    _args_cls: ClassVar[type] = InstallArgs

    async def _execute(self, ctx: SlashCommandContext, args: InstallArgs) -> CommandResult:
        gateway = ctx.gateway
        if not gateway.is_git_available():
            return CommandResult.error("`git` is not on PATH. Install git, then retry.")

        # Resolve @<marketplace>/<plugin> to a concrete (url, subdir,
        # ref) tuple. Bare URLs skip this and install at the clone
        # root with no subdir.
        url = args.target
        subdir: str | None = None
        effective_ref = args.ref
        if args.target.startswith("@"):
            resolved = gateway.resolve_install_ref(args.target)
            if resolved is None:
                return CommandResult.error(
                    f"Could not resolve '{args.target}'. Either no marketplace "
                    "with that name is registered, or it doesn't contain a "
                    "plugin by that name. Run `/plugin marketplace list` "
                    "to see registered marketplaces."
                )
            url = resolved.url
            subdir = resolved.subdir
            # Marketplace-supplied ref wins over the branch heuristic;
            # the user's explicit --ref still takes priority.
            if effective_ref is None:
                effective_ref = resolved.ref

        result = gateway.install(url, ref=effective_ref, subdir=subdir)
        if not result.ok:
            return CommandResult.error(result.error)

        # Only hot-reload on success — ordering vs the gateway Result
        # matters (risk note in the design).
        counts = ctx.session.reload_plugins()
        version = f" v{result.version}" if result.version else ""
        via = f" via {args.target}" if args.target.startswith("@") else ""
        return CommandResult.info(
            f"Installed plugin '{result.name}'{version}{via}. "
            f"Active now — {counts.skills} skill(s), "
            f"{counts.agents} agent(s), {counts.hooks} hook(s). "
            f"Any bundled MCP servers are starting in the background."
        )


class UpdateVerb(PluginVerb):
    """Handles ``/plugin update <name> [--ref <ref>]``."""

    name: ClassVar[str] = "update"
    _args_cls: ClassVar[type] = UpdateArgs

    async def _execute(self, ctx: SlashCommandContext, args: UpdateArgs) -> CommandResult:
        gateway = ctx.gateway
        if not gateway.is_git_available():
            return CommandResult.error("`git` is not on PATH. Install git, then retry.")
        result = gateway.update(args.name, ref=args.ref)
        if not result.ok:
            return CommandResult.error(result.error)
        ctx.session.reload_plugins()
        return CommandResult.info(f"Updated '{args.name}' to {result.sha[:12]}. Active now.")


class RemoveVerb(PluginVerb):
    """Handles ``/plugin remove <name>``."""

    name: ClassVar[str] = "remove"
    _args_cls: ClassVar[type] = RemoveArgs

    async def _execute(self, ctx: SlashCommandContext, args: RemoveArgs) -> CommandResult:
        result = ctx.gateway.remove(args.name)
        if not result.ok:
            return CommandResult.error(result.error)
        ctx.session.reload_plugins()
        return CommandResult.info(
            f"Removed '{args.name}'. Skills/agents/hooks/tools no longer "
            f"active; bundled MCP servers are being disconnected."
        )


# ── /plugin marketplace ────────────────────────────────────────────


class MarketplaceAddVerb(PluginVerb):
    """Handles ``/plugin marketplace add <git-url>``."""

    name: ClassVar[str] = "add"
    _args_cls: ClassVar[type] = MarketplaceAddArgs

    async def _execute(self, ctx: SlashCommandContext, args: MarketplaceAddArgs) -> CommandResult:
        result = ctx.gateway.add_marketplace(args.url)
        if not result.ok:
            return CommandResult.error(result.error)
        return CommandResult.info(
            f"Added marketplace '{result.name}' from {args.url} "
            f"({result.plugin_count} plugin(s) catalogued)."
        )


class MarketplaceListVerb(PluginVerb):
    """Handles ``/plugin marketplace list``. No args — ``rest`` is
    ignored, and the base's parse still runs against the empty-arg
    schema below so the verb API stays uniform."""

    name: ClassVar[str] = "list"

    # No-op args schema — inline to keep the "one schema per verb"
    # contract without polluting plugin_schemas.py with an empty
    # model.
    class _NoArgs:
        @classmethod
        def parse(cls, rest: list[str]):
            # ``rest`` intentionally ignored — list takes no args.
            _ = rest
            return cls()

    _args_cls: ClassVar[type] = _NoArgs

    async def _execute(self, ctx: SlashCommandContext, _args) -> CommandResult:
        registry = ctx.gateway.list_marketplaces()
        if not registry.marketplaces:
            return CommandResult.markdown(
                "## Marketplaces\n(none registered — add one via "
                "`/plugin marketplace add <git-url>`)"
            )
        lines = ["## Marketplaces"]
        for m in registry.marketplaces:
            pcount = len(m.cached.plugins) if m.cached else 0
            last = m.last_fetched or "never"
            lines.append(f"- **{m.name}** · {pcount} plugin(s) · last fetched {last}\n  - {m.url}")
        return CommandResult.markdown("\n".join(lines))


class MarketplaceRemoveVerb(PluginVerb):
    """Handles ``/plugin marketplace remove <name>``."""

    name: ClassVar[str] = "remove"
    _args_cls: ClassVar[type] = MarketplaceRemoveArgs

    async def _execute(
        self, ctx: SlashCommandContext, args: MarketplaceRemoveArgs
    ) -> CommandResult:
        if not ctx.gateway.remove_marketplace(args.name):
            return CommandResult.error(f"No marketplace named '{args.name}' is registered.")
        return CommandResult.info(
            f"Unregistered marketplace '{args.name}'. Installed plugins from it remain installed."
        )


class MarketplaceRefreshVerb(PluginVerb):
    """Handles ``/plugin marketplace refresh [<name>]``.

    Named-refresh returns an ``info`` result; bulk-refresh returns a
    ``markdown`` block (tests assert on that split). The bulk path
    delegates to :class:`BulkRefreshRunner` — the verb itself stays
    a thin dispatch."""

    name: ClassVar[str] = "refresh"
    _args_cls: ClassVar[type] = MarketplaceRefreshArgs

    async def _execute(
        self, ctx: SlashCommandContext, args: MarketplaceRefreshArgs
    ) -> CommandResult:
        if args.name:
            return self._refresh_one(ctx, args.name)
        return self._refresh_all(ctx)

    @staticmethod
    def _refresh_one(ctx: SlashCommandContext, name: str) -> CommandResult:
        result = ctx.gateway.refresh_marketplace(name)
        if result.not_found:
            return CommandResult.error(f"No marketplace named '{name}' is registered.")
        if not result.ok:
            return CommandResult.error(result.error)
        return CommandResult.info(f"Refreshed '{result.name}' ({result.plugin_count} plugin(s)).")

    @staticmethod
    def _refresh_all(ctx: SlashCommandContext) -> CommandResult:
        registry = ctx.gateway.list_marketplaces()
        runner = BulkRefreshRunner(ctx.gateway)
        summary = runner.run(registry)
        if summary.is_empty():
            return CommandResult.info("No marketplaces to refresh.")
        return CommandResult.markdown(summary.to_markdown())


# ── /plugins enable / disable ──────────────────────────────────────


class _PluginsToggleBase(PluginVerb):
    """Shared body for :class:`PluginsEnableVerb` and
    :class:`PluginsDisableVerb`. Two concrete subclasses (not one
    parametrised class) so the verb dispatch is pure polymorphism —
    no ``if mode == 'enable'`` inside a verb body. The tail-message
    variation is captured on the subclass as ``_tail`` and
    ``_verb_past_tense``.
    """

    _args_cls: ClassVar[type] = PluginsToggleArgs

    #: Set-membership predicate. Overridden per subclass so
    #: enable/disable dispatch is polymorphic, not a branch on
    #: ``self.name``.
    _target_in_disabled: ClassVar[bool] = False
    _tail: ClassVar[str] = ""
    _verb_past_tense: ClassVar[str] = ""
    _already_message_suffix: ClassVar[str] = ""

    async def run(self, ctx: SlashCommandContext, rest: list[str]) -> CommandResult:
        """Override the base — this family gets its args as a raw
        string (the ``/plugins ...`` command reads them differently
        than the ``/plugin ...`` family which is already tokenised).
        The router builds the raw suffix and hands it in as
        ``rest[0]``."""
        raw_name = rest[0] if rest else ""
        parsed = PluginsToggleArgs.parse(self.name, raw_name)
        if isinstance(parsed, ArgsParseError):
            return CommandResult.error(parsed.message)
        return await self._execute(ctx, parsed)

    async def _execute(self, ctx: SlashCommandContext, args: PluginsToggleArgs) -> CommandResult:
        session = ctx.session
        loader = session.plugin_loader
        state = session.plugin_state

        if loader.get(args.name) is None:
            return CommandResult.error(
                f"No plugin named '{args.name}' is installed. "
                "Run `/plugins` to list installed plugins."
            )
        disabled_set = set(state.disabled)
        currently_disabled = args.name in disabled_set

        # ``_target_in_disabled`` — the state each subclass moves
        # the plugin INTO — polymorphism replaces the pre-refactor
        # ``if subcommand == 'enable': ...`` branch.
        if currently_disabled == self._target_in_disabled:
            return CommandResult.info(
                f"Plugin '{args.name}' is already {self._already_message_suffix}."
            )
        if self._target_in_disabled:
            disabled_set.add(args.name)
        else:
            disabled_set.discard(args.name)
        state.disabled = sorted(disabled_set)
        save_state(state, data_dir=ctx.plugin_data_dir)
        session.reload_plugins()
        return CommandResult.info(f"Plugin '{args.name}' {self._verb_past_tense}. {self._tail}")


class PluginsEnableVerb(_PluginsToggleBase):
    """Handles ``/plugins enable <name>``."""

    name: ClassVar[str] = "enable"
    _target_in_disabled: ClassVar[bool] = False
    _already_message_suffix: ClassVar[str] = "enabled"
    _verb_past_tense: ClassVar[str] = "enabled"
    _tail: ClassVar[str] = (
        "Its skills/agents/hooks/tools are active; any "
        "bundled MCP servers are starting in the background."
    )


class PluginsDisableVerb(_PluginsToggleBase):
    """Handles ``/plugins disable <name>``."""

    name: ClassVar[str] = "disable"
    _target_in_disabled: ClassVar[bool] = True
    _already_message_suffix: ClassVar[str] = "disabled"
    _verb_past_tense: ClassVar[str] = "disabled"
    _tail: ClassVar[str] = (
        "Its skills/agents/hooks/tools are no longer active; "
        "any bundled MCP servers are being disconnected."
    )


__all__ = [
    "PluginVerb",
    "InstallVerb",
    "UpdateVerb",
    "RemoveVerb",
    "MarketplaceAddVerb",
    "MarketplaceListVerb",
    "MarketplaceRemoveVerb",
    "MarketplaceRefreshVerb",
    "PluginsEnableVerb",
    "PluginsDisableVerb",
]
