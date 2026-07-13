"""``/plugin`` and ``/plugins`` slash commands.

Extracted from :mod:`ember_code.backend.command_handler` — the
plugin-management command family, roughly 290 LoC of the god-file.

Three entry points here:

* :func:`cmd_plugin` — the ``/plugin`` command (install, update,
  remove, marketplace management).
* :func:`cmd_plugin_marketplace` — the ``/plugin marketplace …``
  subcommand family, split out for legibility.
* :func:`cmd_plugins` — the ``/plugins`` command (open the panel
  or toggle enable/disable directly).

Each function is a plain free function taking the
``CommandHandler`` as an explicit argument; ``command_handler``
keeps thin wrapper methods so the dispatch table entries stay
unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.core.plugins.git import GitError
from ember_code.core.plugins.installer import PluginError
from ember_code.core.plugins.state import save_state

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.backend.command_handler import CommandResult


async def cmd_plugin(handler: "CommandHandler", args: str) -> "CommandResult":
    """Install, update, remove plugins; manage marketplaces.

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
    # Look up patchable symbols via the ``command_handler`` module so
    # tests that patch ``ember_code.backend.command_handler.PluginInstaller``
    # / ``resolve_install_ref`` propagate here. Same shim reason the
    # plugin symbols are re-exported from ``command_handler``.
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult

    parts = args.strip().split()
    if not parts:
        return CommandResult.error(
            "Usage: /plugin install <git-url|@marketplace/plugin> | "
            "/plugin update <name> | /plugin remove <name> | "
            "/plugin marketplace add|list|remove|refresh"
        )

    subcommand = parts[0].lower()
    rest = parts[1:]
    data_dir = handler._session.settings.storage.data_dir

    # ── Marketplace management ────────────────────────────────
    if subcommand == "marketplace":
        return await cmd_plugin_marketplace(handler, rest, data_dir)

    # ── install / update / remove ────────────────────────────
    # Extract --ref <value> from anywhere after the subcommand.
    ref: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--ref" and i + 1 < len(rest):
            ref = rest[i + 1]
            i += 2
            continue
        positional.append(rest[i])
        i += 1

    installer = _handler.PluginInstaller(data_dir=data_dir)

    if subcommand == "install":
        if len(positional) != 1:
            return CommandResult.error(
                "Usage: /plugin install <git-url|@marketplace/plugin> [--ref <ref>]"
            )
        target = positional[0]
        if not installer.is_git_available():
            return CommandResult.error("`git` is not on PATH. Install git, then retry.")

        # Resolve marketplace ref to a clone-shaped spec (URL +
        # optional subdir + ref). Bare URLs skip this — they're
        # installed at the clone root with no subdir.
        url = target
        subdir: str | None = None
        mkt_meta = None
        if target.startswith("@"):
            resolved = _handler.resolve_install_ref(target, data_dir=data_dir)
            if resolved is None:
                return CommandResult.error(
                    f"Could not resolve '{target}'. Either no marketplace "
                    "with that name is registered, or it doesn't contain a "
                    "plugin by that name. Run `/plugin marketplace list` "
                    "to see registered marketplaces."
                )
            resolved_source, mkt_meta = resolved
            url = resolved_source.url
            subdir = resolved_source.subdir
            # Marketplace-supplied ref/sha wins over the branch
            # heuristic; the user's explicit --ref still takes
            # priority and is checked first.
            if ref is None:
                ref = resolved_source.ref

        try:
            manifest = installer.install(url, ref=ref, subdir=subdir)
        except GitError as e:
            return CommandResult.error(f"git error: {e}")
        except PluginError as e:
            return CommandResult.error(str(e))
        version = f" v{manifest.version}" if manifest.version else ""
        via = f" via {target}" if target.startswith("@") else ""
        # Hot-reload the new plugin's contents into the live session
        # so the user can use its skills/agents/hooks immediately.
        counts = handler._session.reload_plugins()
        return CommandResult.info(
            f"Installed plugin '{manifest.name}'{version}{via}. "
            f"Active now — {counts.skills} skill(s), "
            f"{counts.agents} agent(s), {counts.hooks} hook(s). "
            f"Any bundled MCP servers are starting in the background."
        )

    if subcommand == "update":
        if len(positional) != 1:
            return CommandResult.error("Usage: /plugin update <name> [--ref <ref>]")
        name = positional[0]
        if not installer.is_git_available():
            return CommandResult.error("`git` is not on PATH. Install git, then retry.")
        try:
            new_sha = installer.update(name, ref=ref)
        except GitError as e:
            return CommandResult.error(f"git error: {e}")
        except PluginError as e:
            return CommandResult.error(str(e))
        handler._session.reload_plugins()
        return CommandResult.info(f"Updated '{name}' to {new_sha[:12]}. Active now.")

    if subcommand == "remove":
        if len(positional) != 1:
            return CommandResult.error("Usage: /plugin remove <name>")
        name = positional[0]
        try:
            installer.remove(name)
        except PluginError as e:
            return CommandResult.error(str(e))
        handler._session.reload_plugins()
        return CommandResult.info(
            f"Removed '{name}'. Skills/agents/hooks/tools no longer "
            f"active; bundled MCP servers are being disconnected."
        )

    return CommandResult.error(
        f"Unknown /plugin subcommand: '{subcommand}'. Use install / "
        "update / remove / marketplace."
    )


async def cmd_plugin_marketplace(
    handler: "CommandHandler",
    rest: list[str],
    data_dir: str,
) -> "CommandResult":
    """Handle the ``/plugin marketplace …`` family of subcommands."""
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult

    if not rest:
        return CommandResult.error(
            "Usage: /plugin marketplace add <url> | list | remove <name> | refresh [<name>]"
        )
    action = rest[0].lower()
    action_rest = rest[1:]

    if action == "add":
        if len(action_rest) != 1:
            return CommandResult.error("Usage: /plugin marketplace add <git-url>")
        url = action_rest[0]
        try:
            entry = _handler.add_marketplace(url, data_dir=data_dir)
        except GitError as e:
            return CommandResult.error(f"git error: {e}")
        except (ValueError, Exception) as e:
            return CommandResult.error(f"Failed to add marketplace: {e}")
        count = len(entry.cached.plugins) if entry.cached else 0
        return CommandResult.info(
            f"Added marketplace '{entry.name}' from {url} ({count} plugin(s) catalogued)."
        )

    if action == "list":
        registry = _handler.load_registry(data_dir=data_dir)
        if not registry.marketplaces:
            return CommandResult.markdown(
                "## Marketplaces\n(none registered — add one via "
                "`/plugin marketplace add <git-url>`)"
            )
        lines = ["## Marketplaces"]
        for m in registry.marketplaces:
            pcount = len(m.cached.plugins) if m.cached else 0
            last = m.last_fetched or "never"
            lines.append(
                f"- **{m.name}** · {pcount} plugin(s) · last fetched {last}\n  - {m.url}"
            )
        return CommandResult.markdown("\n".join(lines))

    if action == "remove":
        if len(action_rest) != 1:
            return CommandResult.error("Usage: /plugin marketplace remove <name>")
        name = action_rest[0]
        if not _handler.remove_marketplace(name, data_dir=data_dir):
            return CommandResult.error(f"No marketplace named '{name}' is registered.")
        return CommandResult.info(
            f"Unregistered marketplace '{name}'. Installed plugins from it remain installed."
        )

    if action == "refresh":
        if len(action_rest) > 1:
            return CommandResult.error("Usage: /plugin marketplace refresh [<name>]")
        if action_rest:
            name = action_rest[0]
            try:
                refreshed = _handler.refresh_marketplace(name, data_dir=data_dir)
            except GitError as e:
                return CommandResult.error(f"git error: {e}")
            except Exception as e:
                return CommandResult.error(f"Refresh failed: {e}")
            if refreshed is None:
                return CommandResult.error(f"No marketplace named '{name}' is registered.")
            count = len(refreshed.cached.plugins) if refreshed.cached else 0
            return CommandResult.info(f"Refreshed '{refreshed.name}' ({count} plugin(s)).")

        # Refresh all.
        registry = _handler.load_registry(data_dir=data_dir)
        results: list[str] = []
        for m in registry.marketplaces:
            try:
                _handler.refresh_marketplace(m.name, data_dir=data_dir)
                results.append(f"- {m.name}: ok")
            except Exception as e:
                results.append(f"- {m.name}: failed ({e})")
        if not results:
            return CommandResult.info("No marketplaces to refresh.")
        return CommandResult.markdown("## Marketplace refresh\n" + "\n".join(results))

    return CommandResult.error(
        f"Unknown /plugin marketplace action: '{action}'. Use add / list / remove / refresh."
    )


async def cmd_plugins(handler: "CommandHandler", args: str) -> "CommandResult":
    """Open the plugins panel, or toggle enable/disable directly.

    Forms:
      /plugins                    — open the plugins TUI panel
      /plugins enable <name>      — enable a disabled plugin (no panel)
      /plugins disable <name>     — disable an enabled plugin (no panel)

    Enable/disable persist to ``~/.ember/plugins.json`` and take
    effect on the next session start (and hot-reload for the
    current one).
    """
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult

    loader = getattr(handler._session, "plugin_loader", None)
    state = getattr(handler._session, "plugin_state", None)
    if loader is None or state is None:
        return CommandResult.info("Plugins not initialized.")

    parts = args.strip().split(None, 1)
    subcommand = parts[0].lower() if parts else ""
    name = parts[1].strip() if len(parts) > 1 else ""

    # No subcommand → open the TUI panel.
    if not subcommand:
        return CommandResult.plugins()

    if subcommand in ("enable", "disable"):
        if not name:
            return CommandResult.error(f"Usage: /plugins {subcommand} <plugin-name>")
        if loader.get(name) is None:
            return CommandResult.error(
                f"No plugin named '{name}' is installed. "
                "Run `/plugins` to list installed plugins."
            )
        disabled_set = set(state.disabled)
        if subcommand == "enable":
            if name not in disabled_set:
                return CommandResult.info(f"Plugin '{name}' is already enabled.")
            disabled_set.discard(name)
        else:  # disable
            if name in disabled_set:
                return CommandResult.info(f"Plugin '{name}' is already disabled.")
            disabled_set.add(name)
        state.disabled = sorted(disabled_set)
        save_state(state, data_dir=handler._session.settings.storage.data_dir)
        # Hot-reload picks up the new disabled set and re-applies
        # skills/agents/hooks accordingly.
        handler._session.reload_plugins()
        if subcommand == "enable":
            tail = (
                "Its skills/agents/hooks/tools are active; any "
                "bundled MCP servers are starting in the background."
            )
        else:
            tail = (
                "Its skills/agents/hooks/tools are no longer active; "
                "any bundled MCP servers are being disconnected."
            )
        return CommandResult.info(f"Plugin '{name}' {subcommand}d. {tail}")

    return CommandResult.error(
        f"Unknown /plugins subcommand: '{subcommand}'. "
        "Use `enable` or `disable`, or run `/plugins` alone to open "
        "the panel."
    )
