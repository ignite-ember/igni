"""Command handler — processes slash commands for the TUI."""

import logging
from typing import TYPE_CHECKING, Any

from ember_code.protocol.messages import CommandAction, CommandResultKind

logger = logging.getLogger(__name__)


def _open_in_browser(url: str) -> None:
    """Best-effort open in browser; failures are logged, never raised."""
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception as exc:  # pragma: no cover — platform-dependent
        logger.info("could not open browser for %s: %s", url, exc)


SHORTCUT_HELP = (
    "## Keyboard Shortcuts\n"
    "- `Enter` — send message\n"
    "- `\\` + `Enter` — new line\n"
    "- `Ctrl+D` — quit\n"
    "- `Ctrl+L` — clear screen\n"
    "- `Ctrl+O` — expand/collapse all messages\n"
    "- `Ctrl+V` — toggle verbose mode\n"
    "- `Up/Down` — input history\n"
    "- `Escape` — cancel\n"
)

if TYPE_CHECKING:
    from ember_code.core.session import Session


class CommandResult:
    """Result of executing a slash command."""

    def __init__(
        self,
        kind: str = "markdown",
        content: str = "",
        action: str | None = None,
        display_content: str = "",
    ):
        self.kind = kind  # "markdown", "info", "error", "action"
        self.content = content
        self.action = action  # "quit", "clear", None
        # See protocol.messages.CommandResult for the rationale.
        # Only the loop's ``run_prompt`` flow sets this today.
        self.display_content = display_content

    @classmethod
    def markdown(cls, text: str) -> "CommandResult":
        return cls(kind=CommandResultKind.MARKDOWN, content=text)

    @classmethod
    def info(cls, text: str) -> "CommandResult":
        return cls(kind=CommandResultKind.INFO, content=text)

    @classmethod
    def error(cls, text: str) -> "CommandResult":
        return cls(kind=CommandResultKind.ERROR, content=text)

    @classmethod
    def quit(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.QUIT)

    @classmethod
    def clear(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.CLEAR)

    @classmethod
    def sessions(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.SESSIONS)

    @classmethod
    def model(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.MODEL)

    @classmethod
    def login(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.LOGIN)

    @classmethod
    def mcp(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.MCP)

    @classmethod
    def plugins(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.PLUGINS)

    @classmethod
    def agents(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.AGENTS)

    @classmethod
    def skills(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.SKILLS)

    @classmethod
    def knowledge(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.KNOWLEDGE)

    @classmethod
    def codeindex(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.CODEINDEX)

    @classmethod
    def hooks(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.HOOKS)

    @classmethod
    def loop(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.LOOP)


class CommandHandler:
    """Handles slash commands, decoupled from the TUI rendering.

    Each command returns a ``CommandResult`` that the app renders
    appropriately.
    """

    def __init__(self, session: "Session"):
        self._session = session

    async def handle(self, command: str) -> "CommandResult":
        """Dispatch a slash command and return its result."""
        stripped = command.strip()
        cmd = stripped.split()[0].lower()
        args = stripped[len(cmd) :].strip()

        handler = self._COMMANDS.get(cmd)
        if handler:
            return await handler(self, args)

        # Try skill match
        return await self._handle_skill(stripped)

    # ── Commands ──────────────────────────────────────────────────

    async def _cmd_quit(self, _args: str) -> "CommandResult":
        return CommandResult.quit()

    _HELP_TOPICS: dict[str, str] = {
        "schedule": (
            "## Schedule\n\n"
            "Schedule tasks for later or recurring execution.\n\n"
            "**Commands:**\n"
            "- `/schedule` — list pending tasks\n"
            "- `/schedule all` — include completed and cancelled\n"
            "- `/schedule add <description> at <time>` — one-shot task\n"
            "- `/schedule add <description> in <duration>` — relative time\n"
            "- `/schedule add <description> every <interval>` — recurring\n"
            "- `/schedule show <id>` — show task details\n"
            "- `/schedule cancel <id>` — cancel a task\n\n"
            "**Time formats:**\n"
            "- One-shot: `at 5pm`, `at 3:30`, `tomorrow`, `tomorrow at 9am`, `2026-12-25 14:00`\n"
            "- Relative: `in 30 minutes`, `in 2 hours`, `in 1 day`\n"
            "- Recurring: `every 2 hours`, `every 30 minutes`, `daily`, `daily at 9am`, `hourly`, `weekly`\n\n"
            "**Examples:**\n"
            "```\n"
            "/schedule add review code at 5pm\n"
            "/schedule add run tests in 30 minutes\n"
            "/schedule add check deps daily\n"
            "/schedule add run linter every 2 hours\n"
            "```"
        ),
        "loop": (
            "## Loop\n\n"
            "Repeat a prompt over and over in the current session. The "
            "next iteration starts immediately after the previous one "
            'ends — no gap. Useful for *"do X for each of A, B, C"*, '
            '*"keep fixing failures until the tests pass"*, *"go '
            'through these one at a time"*.\n\n'
            "**Commands:**\n"
            "- `/loop <prompt>` — start (default cap: 30 iterations)\n"
            "- `/loop <N> <prompt>` — start with explicit cap N\n"
            "- `/loop stop` — cancel the active loop\n"
            "- `/loop` — show status (active loop, iterations done/remaining)\n\n"
            "**Stops on:**\n"
            "- `/loop stop` from the user\n"
            "- Any non-`/loop` user input (treated as an interrupt)\n"
            "- The iteration cap (default 30, hard limit 200)\n"
            "- The agent calling `loop_stop()` mid-conversation\n\n"
            "**Plain-language control:** the agent has `loop_start` / "
            '`loop_stop` tools, so you can say *"loop this 5 times"* '
            'or *"stop the loop"* in normal chat and the agent will '
            "translate to the right call.\n\n"
            "**Differs from `/schedule`:** schedule fires on a cron clock "
            "and runs headlessly. `/loop` fires immediately and streams "
            "every iteration live in your TUI."
        ),
        "plugins": (
            "## Plugins\n\n"
            "Claude-Code-compatible plugin support. A plugin is a "
            "directory bundling skills, agents, hooks, MCP servers, "
            "and/or custom tools that activate together. Plugins "
            "built for Claude Code work in Ember unchanged.\n\n"
            "**Discovery roots (highest priority last):**\n"
            "- `~/.claude/plugins/` (Claude user-global)\n"
            "- `~/.ember/plugins/` (ember user-global — where `/plugin install` lands)\n"
            "- `<project>/.claude/plugins/`\n"
            "- `<project>/.ember/plugins/`\n\n"
            "**Daily commands:**\n"
            "- `/plugins` — open the TUI panel (browse, toggle, install)\n"
            "- `/plugins enable <name>` / `/plugins disable <name>` — toggle without opening the panel\n"
            "- `/plugin install <git-url>` — install from a git URL into `~/.ember/plugins/`\n"
            "- `/plugin install @<marketplace>/<plugin>` — install via marketplace\n"
            "- `/plugin install <url> --ref <branch|tag|sha>` — pin at install time\n"
            "- `/plugin update <name>` — fetch + reset to origin's HEAD\n"
            "- `/plugin remove <name>` — uninstall (deletes the plugin dir)\n\n"
            "**Marketplaces (Claude-Code-compatible catalogs):**\n"
            "- `/plugin marketplace add <git-url>` — register a marketplace\n"
            "- `/plugin marketplace list` — show registered marketplaces\n"
            "- `/plugin marketplace remove <name>` — unregister (installed plugins remain)\n"
            "- `/plugin marketplace refresh [<name>]` — re-fetch one or all\n\n"
            "Enable / disable / install / update / remove all take effect "
            "on next session start. See [Plugins](PLUGINS.md) for the "
            "full guide."
        ),
        "agents": (
            "## Agents\n\n"
            "Agents are specialist roles with tools and system prompts.\n\n"
            "**Commands:**\n"
            "- `/agents` — list all loaded agents with tools\n"
            "- `/agents ephemeral` — list dynamically created agents\n"
            "- `/agents promote <name>` — save ephemeral agent permanently\n"
            "- `/agents discard <name>` — delete an ephemeral agent\n\n"
            "**Create agents:** add `.md` files to `.ember/agents/`\n"
            "**Customize:** edit any agent in `.ember/agents/` to change its behavior"
        ),
        "knowledge": (
            "## Knowledge Base\n\n"
            "Store and search project knowledge with embeddings.\n\n"
            "**Commands:**\n"
            "- `/knowledge` — show status (collection, doc count, embedder)\n"
            "- `/knowledge add <url>` — add a URL\n"
            "- `/knowledge add <path>` — add a file or directory\n"
            "- `/knowledge add <text>` — add inline text\n"
            "- `/knowledge search <query>` — search the knowledge base\n"
            "- `/sync-knowledge` — sync between git file and vector DB"
        ),
        "codeindex": (
            "## CodeIndex\n\n"
            "Semantic code intelligence over your repo. The Ember GitHub App "
            "indexes every commit on the server and ships per-commit changesets "
            "to your machine, where they're applied to a local Chroma index. "
            "Search runs entirely locally — your code summaries never leave "
            "your laptop after the initial fetch.\n\n"
            "**Setup (one-time):**\n"
            "- `/codeindex install` — open the GitHub App install page,\n"
            "  pre-pointed at your current repo. Click **Install** and you're done.\n\n"
            "**Daily commands:**\n"
            "- `/codeindex sync` — pull and apply the changeset for the current commit\n"
            "- `/codeindex resync` — wipe local state and re-pull a full snapshot\n"
            "- `/codeindex search <query>` — semantic search the indexed commit\n"
            "- `/codeindex item <id>` — full details for one item\n"
            "- `/codeindex commits` — list locally-indexed commits\n"
            "- `/codeindex clean` — drop stale, non-branch commits\n"
            "- `/codeindex status` — show local HEAD, last-synced sha, install state\n\n"
            "**Auto-sync:**\n"
            "Sync fires on app startup, on `/clear`, and whenever your local "
            "HEAD moves (after `git pull`, branch switch, etc.) — no manual "
            "trigger needed in the steady state."
        ),
        "memory": (
            "## Memory & Learning\n\n"
            "Ember Code learns your preferences automatically from conversations.\n\n"
            "**Commands:**\n"
            "- `/memory` — show what Ember has learned about you\n"
            "- `/memory optimize` — consolidate memories\n\n"
            "**What gets learned:**\n"
            "- Your name and how you prefer to be addressed\n"
            "- Tool and framework preferences (pytest, ruff, Pydantic, etc.)\n"
            "- Project structure conventions (src/ layout, etc.)\n"
            "- Coding style preferences (type hints, etc.)\n\n"
            "Learning happens in the background after each response."
        ),
        "mcp": (
            "## MCP Servers\n\n"
            "Connect external tools via the Model Context Protocol.\n\n"
            "**Commands:**\n"
            "- `/mcp` — open the MCP panel (browse, connect, disconnect)\n\n"
            "**Configuration:** add servers to `.mcp.json`:\n"
            "```json\n"
            '{"mcpServers": {"name": {"type": "stdio", "command": "npx", "args": [...]}}}\n'
            "```\n"
            "**Transports:** `stdio` and `sse` supported\n"
            "**Panel controls:** Space toggle, Enter expand tools, Escape close"
        ),
        "hooks": (
            "## Hooks\n\n"
            "Run a command or POST to a URL when an event fires "
            "(before a tool runs, when a session starts, when the "
            "user submits a prompt, etc.). Hooks gate or audit "
            "behavior — return `should_continue=false` from a "
            "command hook to block the triggering action.\n\n"
            "**Commands:**\n"
            "- `/hooks` — open the hooks panel (group by event, expand for full command, `R` to reload)\n"
            "- `/hooks list` — markdown list to chat (scripting-friendly)\n"
            "- `/hooks reload` — re-read settings files (chat output)\n\n"
            "**Events** (see `core/hooks/events.py` for the full enum):\n"
            "- `PreToolUse`, `PostToolUse`, `PostToolUseFailure`\n"
            "- `UserPromptSubmit`, `SessionStart`, `SessionEnd`\n"
            "- `Stop`, `SubagentStart`, `SubagentStop`, `Notification`\n\n"
            "**Defined in** (four-root cascade, last wins):\n"
            "- `~/.ember/settings.json` (global)\n"
            "- `~/.ember/settings.local.json` (global, gitignored)\n"
            "- `.ember/settings.json` (project, committed)\n"
            "- `.ember/settings.local.json` (project, gitignored)\n\n"
            "Plugins also contribute hooks via `<plugin>/hooks/hooks.json`; "
            "plugin hooks are prepended per event so project hooks get "
            "the final word.\n\n"
            "**Hook config keys:**\n"
            "- `type`: `command` or `http`\n"
            "- `command` / `url`: what to run\n"
            "- `headers`: HTTP headers (for `http` type)\n"
            "- `matcher`: regex over tool name (empty = all)\n"
            "- `timeout`: ms before the hook is killed (default 10000)\n"
            "- `background`: fire-and-forget (don't block the agent)"
        ),
        "shortcuts": SHORTCUT_HELP,
    }

    async def _cmd_help(self, args: str) -> "CommandResult":
        topic = args.strip().lower()

        # Topic-specific help
        if topic and topic in self._HELP_TOPICS:
            return CommandResult.markdown(self._HELP_TOPICS[topic])

        # List available topics if unknown
        if topic:
            available = ", ".join(sorted(self._HELP_TOPICS.keys()))
            return CommandResult.error(f"Unknown help topic: {topic}. Available: {available}")

        # No topic: show interactive panel
        return CommandResult(kind=CommandResultKind.INFO, content="", action=CommandAction.HELP)

    async def _cmd_agents(self, args: str) -> "CommandResult":
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        if subcommand == "promote":
            # Missing name is a user error — without this guard the
            # call falls through to opening the panel, silently
            # ignoring the user's intent.
            if not sub_args:
                return CommandResult.error("Usage: /agents promote <name>")
            name = sub_args.strip()
            try:
                dest = self._session.pool.promote_ephemeral(name, self._session.project_dir)
                return CommandResult.info(f"Promoted '{name}' to {dest}")
            except (KeyError, ValueError, RuntimeError) as e:
                return CommandResult.error(str(e))

        if subcommand == "discard":
            if not sub_args:
                return CommandResult.error("Usage: /agents discard <name>")
            name = sub_args.strip()
            try:
                self._session.pool.discard_ephemeral(name)
                return CommandResult.info(f"Discarded ephemeral agent '{name}'.")
            except (KeyError, ValueError, RuntimeError) as e:
                return CommandResult.error(str(e))

        if subcommand == "ephemeral":
            agents = self._session.pool.list_ephemeral()
            if not agents:
                return CommandResult.info("No ephemeral agents.")
            lines = "## Ephemeral Agents\n"
            for defn in agents:
                tools = ", ".join(defn.tools) if defn.tools else "none"
                lines += f"- **{defn.name}** — {defn.description}\n  tools: {tools}\n"
            lines += "\n*`/agents promote <name>` to save · `/agents discard <name>` to remove*\n"
            return CommandResult.markdown(lines)

        # Default (no subcommand): open the TUI panel.
        return CommandResult.agents()

    async def _cmd_skills(self, _args: str) -> "CommandResult":
        """Open the skills TUI panel.

        The panel surfaces description, version, source dir, argument
        hint, and an expandable preview of the skill body — strictly
        more information than the old markdown listing. The legacy
        markdown form is gone; consumers that want a text dump should
        scrape the panel data via ``get_skill_details`` over RPC.
        """
        return CommandResult.skills()

    async def _cmd_plugin(self, args: str) -> "CommandResult":
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
        from ember_code.core.plugins.git import GitError as _GitError
        from ember_code.core.plugins.installer import (
            PluginError as _PluginError,
        )
        from ember_code.core.plugins.installer import (
            PluginInstaller as _PluginInstaller,
        )
        from ember_code.core.plugins.marketplaces import (
            resolve_install_ref as _resolve_install_ref,
        )

        parts = args.strip().split()
        if not parts:
            return CommandResult.error(
                "Usage: /plugin install <git-url|@marketplace/plugin> | "
                "/plugin update <name> | /plugin remove <name> | "
                "/plugin marketplace add|list|remove|refresh"
            )

        subcommand = parts[0].lower()
        rest = parts[1:]
        data_dir = self._session.settings.storage.data_dir

        # ── Marketplace management ────────────────────────────────
        if subcommand == "marketplace":
            return await self._cmd_plugin_marketplace(rest, data_dir)

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

        installer = _PluginInstaller(data_dir=data_dir)

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
                resolved = _resolve_install_ref(target, data_dir=data_dir)
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
            except _GitError as e:
                return CommandResult.error(f"git error: {e}")
            except _PluginError as e:
                return CommandResult.error(str(e))
            version = f" v{manifest.version}" if manifest.version else ""
            via = f" via {target}" if target.startswith("@") else ""
            # Hot-reload the new plugin's contents into the live
            # session so the user can use its skills/agents/hooks
            # immediately. See ``Session.reload_plugins`` for the
            # full set of subsystems refreshed.
            counts = self._session.reload_plugins()
            return CommandResult.info(
                f"Installed plugin '{manifest.name}'{version}{via}. "
                f"Active now — {counts['skills']} skill(s), "
                f"{counts['agents']} agent(s), {counts['hooks']} hook(s). "
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
            except _GitError as e:
                return CommandResult.error(f"git error: {e}")
            except _PluginError as e:
                return CommandResult.error(str(e))
            self._session.reload_plugins()
            return CommandResult.info(f"Updated '{name}' to {new_sha[:12]}. Active now.")

        if subcommand == "remove":
            if len(positional) != 1:
                return CommandResult.error("Usage: /plugin remove <name>")
            name = positional[0]
            try:
                installer.remove(name)
            except _PluginError as e:
                return CommandResult.error(str(e))
            self._session.reload_plugins()
            return CommandResult.info(
                f"Removed '{name}'. Skills/agents/hooks/tools no longer "
                f"active; bundled MCP servers are being disconnected."
            )

        return CommandResult.error(
            f"Unknown /plugin subcommand: '{subcommand}'. Use install / "
            "update / remove / marketplace."
        )

    async def _cmd_plugin_marketplace(
        self,
        rest: list[str],
        data_dir: str,
    ) -> "CommandResult":
        """Handle the ``/plugin marketplace …`` family of subcommands.

        Split out from ``_cmd_plugin`` so the parent dispatch stays
        legible and so test coverage for marketplace flows can target
        this method directly.
        """
        from ember_code.core.plugins.git import GitError as _GitError
        from ember_code.core.plugins.marketplaces import (
            add_marketplace as _add,
        )
        from ember_code.core.plugins.marketplaces import (
            load_registry as _load,
        )
        from ember_code.core.plugins.marketplaces import (
            refresh_marketplace as _refresh,
        )
        from ember_code.core.plugins.marketplaces import (
            remove_marketplace as _remove,
        )

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
                entry = _add(url, data_dir=data_dir)
            except _GitError as e:
                return CommandResult.error(f"git error: {e}")
            except (ValueError, Exception) as e:
                return CommandResult.error(f"Failed to add marketplace: {e}")
            count = len(entry.cached.plugins) if entry.cached else 0
            return CommandResult.info(
                f"Added marketplace '{entry.name}' from {url} ({count} plugin(s) catalogued)."
            )

        if action == "list":
            registry = _load(data_dir=data_dir)
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
            if not _remove(name, data_dir=data_dir):
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
                    refreshed = _refresh(name, data_dir=data_dir)
                except _GitError as e:
                    return CommandResult.error(f"git error: {e}")
                except Exception as e:
                    return CommandResult.error(f"Refresh failed: {e}")
                if refreshed is None:
                    return CommandResult.error(f"No marketplace named '{name}' is registered.")
                count = len(refreshed.cached.plugins) if refreshed.cached else 0
                return CommandResult.info(f"Refreshed '{refreshed.name}' ({count} plugin(s)).")

            # Refresh all.
            registry = _load(data_dir=data_dir)
            results: list[str] = []
            for m in registry.marketplaces:
                try:
                    _refresh(m.name, data_dir=data_dir)
                    results.append(f"- {m.name}: ok")
                except Exception as e:
                    results.append(f"- {m.name}: failed ({e})")
            if not results:
                return CommandResult.info("No marketplaces to refresh.")
            return CommandResult.markdown("## Marketplace refresh\n" + "\n".join(results))

        return CommandResult.error(
            f"Unknown /plugin marketplace action: '{action}'. Use add / list / remove / refresh."
        )

    async def _cmd_plugins(self, args: str) -> "CommandResult":
        """Open the plugins panel, or toggle enable/disable directly.

        Forms:
          /plugins                    — open the plugins TUI panel
          /plugins enable <name>      — enable a disabled plugin (no panel)
          /plugins disable <name>     — disable an enabled plugin (no panel)

        Enable/disable persist to ``~/.ember/plugins.json`` and take
        effect on the next session start.
        """
        loader = getattr(self._session, "plugin_loader", None)
        state = getattr(self._session, "plugin_state", None)
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
            from ember_code.core.plugins.state import save_state

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
            save_state(state, data_dir=self._session.settings.storage.data_dir)
            # Hot-reload picks up the new disabled set and re-applies
            # skills/agents/hooks accordingly. MCP servers are
            # connected (enable) or disconnected (disable) in the
            # background as a side-effect of the apply diff.
            self._session.reload_plugins()
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

    async def _cmd_hooks(self, args: str) -> "CommandResult":
        """Hooks slash command.

        No args → open the interactive TUI panel (browse hooks
        grouped by event, expand for full command/headers, ``R``
        to reload from disk). Explicit subcommands stay as
        scripting fallbacks:

        * ``/hooks reload`` — re-read disk, return count to chat.
        * ``/hooks list`` — markdown list to chat (the legacy
          no-args behavior; preserved for scripting / piping).
        """
        subcommand = args.strip().lower()
        if subcommand == "reload":
            count = self._session.reload_hooks()
            return CommandResult.info(f"Hooks reloaded. {count} hook(s) loaded.")

        if subcommand == "list":
            if not self._session.hooks_map:
                return CommandResult.info("No hooks loaded.")
            lines = "## Hooks\n"
            for event, hook_list in self._session.hooks_map.items():
                for h in hook_list:
                    matcher = f" (matcher: {h.matcher})" if h.matcher else ""
                    lines += f"- **{event}**: `{h.command or h.url}`{matcher}\n"
            return CommandResult.markdown(lines)

        # Default — open the panel.
        return CommandResult.hooks()

    async def _cmd_clear(self, _args: str) -> "CommandResult":
        # Generate new session_id so Agno starts fresh history
        import asyncio
        import uuid

        self._session.session_id = str(uuid.uuid4())[:8]

        # New dialogue → re-pull the changeset for current HEAD
        # (fire-and-forget). Also refresh the codeindex_available flag
        # afterwards so the rebuilt agent's system prompt matches the
        # post-sync chroma state.
        async def _sync_then_refresh() -> None:
            await self._session.code_index_sync.sync_now()
            try:
                self._session.refresh_codeindex_availability()
            except Exception as exc:
                logger.debug("refresh after /clear sync failed (%s)", exc)

        asyncio.create_task(_sync_then_refresh())
        return CommandResult.clear()

    async def _cmd_sessions(self, _args: str) -> "CommandResult":
        return CommandResult.sessions()

    async def _cmd_rename(self, args: str) -> "CommandResult":
        name = args.strip()
        if not name:
            return CommandResult.error("Usage: /rename <new session name>")
        await self._session.persistence.rename(name)
        return CommandResult.info(f"Session renamed to: {name}")

    async def _cmd_memory(self, args: str) -> "CommandResult":
        subcommand = args.strip().lower()

        if subcommand == "optimize":
            result = await self._session.memory_mgr.optimize()
            if "error" in result:
                return CommandResult.error(f"Memory optimization failed: {result['error']}")
            return CommandResult.info(result["message"])

        # Show Learning Machine data — use agent's property which triggers lazy init
        learning = getattr(self._session.main_team, "learning_machine", None)
        if learning is None:
            learning = getattr(self._session, "_learning", None)
        if learning is None:
            return CommandResult.info(
                "Learning is not enabled. Set learning.enabled=true in config."
            )

        sections: list[str] = []
        try:
            # Recall with session_id=None to get cross-session data
            # (user profile, user memory, entity memory)
            data = await learning.arecall(
                user_id=self._session.user_id,
            )
            for store_name, store_data in data.items():
                if not store_data:
                    continue
                title = store_name.replace("_", " ").title()
                lines = f"## {title}\n"

                if store_name == "user_profile":
                    for attr in ("name", "preferred_name", "role", "expertise", "preferences"):
                        val = getattr(store_data, attr, None)
                        if val:
                            lines += f"- **{attr.replace('_', ' ').title()}**: {val}\n"

                elif store_name == "user_memory":
                    memories = getattr(store_data, "memories", []) or []
                    for m in memories:
                        content = m.get("content", "") if isinstance(m, dict) else str(m)
                        if content:
                            lines += f"- {content}\n"

                elif store_name == "session_context":
                    summary = getattr(store_data, "summary", None)
                    if summary:
                        lines += f"{summary}\n"

                elif store_name == "entity_memory":
                    entities = getattr(store_data, "entities", []) or []
                    for e in entities:
                        if isinstance(e, dict):
                            lines += f"- **{e.get('name', '?')}**: {e.get('description', '')}\n"
                        else:
                            lines += f"- {e}\n"

                else:
                    lines += f"{store_data}\n"

                if lines.strip() != f"## {title}":
                    sections.append(lines)
        except Exception:
            pass

        if not sections:
            return CommandResult.info(
                "No learnings stored yet. The agent learns from your conversations automatically."
            )

        return CommandResult.markdown("\n\n".join(sections))

    async def _cmd_knowledge(self, args: str) -> "CommandResult":
        """Handle /knowledge commands: add url|path|text, search, status."""
        mgr = self._session.knowledge_mgr
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        if subcommand == "add" and sub_args:
            if sub_args.startswith(("http://", "https://")):
                result = await mgr.add_url(sub_args)
            elif "/" in sub_args or sub_args.startswith("."):
                result = await mgr.add_path(sub_args)
            else:
                result = await mgr.add(text=sub_args)
            if not result.success:
                return CommandResult.error(result.error)
            return CommandResult.info(result.message)

        if subcommand == "search":
            # Open the panel — the input field there IS the search UI.
            # Pre-populating from the slash command would defeat the
            # purpose: the panel is where users type queries, iterate,
            # and browse results interactively. ``sub_args`` (if any)
            # is ignored on purpose; users continue typing in the panel.
            return CommandResult.knowledge()

        # No subcommand: open the TUI panel. (Status + commands hint
        # were previously printed as markdown — the panel surfaces
        # the same status header and lets the user search / add
        # interactively.) The error path is preserved so users see a
        # clear reason when the base failed to initialize.
        status = await mgr.status()
        if not status.enabled:
            if self._session.settings.knowledge.enabled:
                if self._session._knowledge_error:
                    return CommandResult.error(
                        f"Knowledge failed to load: {self._session._knowledge_error}"
                    )
                return CommandResult.error("Knowledge base failed to initialize.")
            return CommandResult.info(
                "Knowledge base is disabled. Set knowledge.enabled=true in config."
            )
        return CommandResult.knowledge()

    async def _cmd_codeindex(self, args: str) -> "CommandResult":
        """Handle /codeindex commands.

        No-arg invocation opens the TUI status panel (current-commit
        indexed state + sync/clean/install verb keys, with a 2s live
        status poll). Search lives on ``/codeindex search <query>``
        and renders markdown into chat — results are better-suited
        to chat history than to an ephemeral bottom panel.

        The remaining subcommands (``item``, ``commits``, ``clean``,
        ``sync``, ``install``, ``status``) keep their chat output as
        a power-user / scripting fallback.
        """
        index = self._session.code_index
        sync = self._session.code_index_sync
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        if not subcommand:
            return CommandResult.codeindex()

        if subcommand == "search" and sub_args:
            results = await index.search(query=sub_args, limit=10)
            if not results:
                return CommandResult.info("No results.")
            lines = f"## CodeIndex Search ({len(results)} results)\n"
            for i, r in enumerate(results, 1):
                score_str = f"{r.score:.3f}" if r.score is not None else "n/a"
                lines += (
                    f"\n**{i}. {r.name}** (`{r.item_id}`)"
                    f" — {r.path} (score {score_str})\n"
                    f"{r.chunk_preview or ''}\n"
                )
            return CommandResult.markdown(lines)

        if subcommand == "item" and sub_args:
            item = await index.get_item(item_id=sub_args.strip())
            if item is None:
                return CommandResult.error(f"Item {sub_args.strip()} not found.")
            preview = item.content
            if len(preview) > 1500:
                preview = preview[:1500] + "..."
            return CommandResult.markdown(
                f"## {item.name}\n"
                f"- **id:** `{item.item_id}`\n"
                f"- **path:** {item.path}\n"
                f"- **type:** {item.type}\n"
                f"- **commit:** {item.commit}\n\n"
                f"```\n{preview}\n```"
            )

        if subcommand == "commits":
            state = index.manifest.load()
            if not state.commits:
                return CommandResult.info("No commits indexed.")
            lines = f"## Indexed Commits (head: `{state.head or 'none'}`)\n"
            for sha, info in sorted(
                state.commits.items(),
                key=lambda kv: kv[1].last_used_at,
                reverse=True,
            ):
                head_marker = " (HEAD)" if sha == state.head else ""
                branch = f" branches: {', '.join(info.branch_refs)}" if info.branch_refs else ""
                lines += f"\n- `{sha}`{head_marker} — last used {info.last_used_at}{branch}"
            return CommandResult.markdown(lines)

        if subcommand == "clean":
            dropped = await index.clean()
            if not dropped:
                return CommandResult.info("Nothing to clean.")
            return CommandResult.info(f"Dropped {len(dropped)} commit(s): {', '.join(dropped)}")

        if subcommand == "sync":
            target_sha = sub_args or None
            result = await sync.sync_now(sha=target_sha)
            # Re-derive ``codeindex_available`` so the agent's prompt
            # matches the post-sync chroma state (see
            # ``Session.refresh_codeindex_availability``).
            try:
                self._session.refresh_codeindex_availability()
            except Exception as exc:
                logger.debug("refresh after /codeindex sync failed (%s)", exc)
            if result.link_start_url:
                _open_in_browser(result.link_start_url)
                lines = (
                    f"### CodeIndex needs setup\n"
                    f"{result.reason}\n\n"
                    f"Opening your browser to:\n"
                    f"`{result.link_start_url}`\n\n"
                    f"After the GitHub UI finishes, run `/codeindex sync` again."
                )
                return CommandResult.markdown(lines)
            if result.skipped:
                return CommandResult.info(f"Sync skipped: {result.reason}")
            if result.error:
                return CommandResult.error(
                    f"Sync of {result.commit_sha[:8] if result.commit_sha else '?'} failed: {result.error}"
                )
            stats = result.stats
            short_sha = result.commit_sha[:8] if result.commit_sha else "?"
            return CommandResult.info(
                f"Synced {short_sha}: "
                f"{stats.items_upserted} upserts, {stats.items_deleted} deletes, "
                f"{stats.references_upserted} refs."
            )

        if subcommand == "resync":
            # Wipe the local chroma for the target sha and pull a fresh
            # snapshot. Used when the local index drifts from the cloud
            # definition — e.g. an earlier sync took the delta path with
            # an absent parent and stored only the diff's items.
            target_sha = sub_args or sync.current_sha()
            if not target_sha:
                return CommandResult.error("Not a git repository — pass an explicit sha.")
            forgot = await index.forget_commit(target_sha)
            result = await sync.sync_now(sha=target_sha, force_snapshot=True)
            # Same as ``/codeindex sync`` — keep the agent's prompt
            # in sync with the chroma state ``forget_commit`` +
            # ``sync_now`` just established.
            try:
                self._session.refresh_codeindex_availability()
            except Exception as exc:
                logger.debug("refresh after /codeindex resync failed (%s)", exc)
            short_sha = (result.commit_sha or target_sha)[:8]
            if result.skipped:
                prefix = "Wiped local index; " if forgot else ""
                return CommandResult.info(f"{prefix}sync skipped: {result.reason}")
            if result.error:
                return CommandResult.error(f"Resync of {short_sha} failed: {result.error}")
            stats = result.stats
            prefix = "Wiped local index. " if forgot else ""
            return CommandResult.info(
                f"{prefix}Resynced {short_sha} via snapshot: "
                f"{stats.items_upserted} upserts, "
                f"{stats.references_upserted} refs."
            )

        if subcommand == "install":
            # Explicit "open the install page for this repo" entry point —
            # useful when `sync` already succeeded but the user wants to
            # add a sibling repo, or revisit the install screen.
            resolver = sync.resolver
            if resolver is None:
                return CommandResult.error("Resolver not available.")
            resolved = await resolver.resolve(force=True)
            if resolved is None:
                return CommandResult.error(
                    "Could not reach Ember Cloud — check `/login` and `api_url`."
                )
            if not resolved.needs_install:
                return CommandResult.info(
                    f"This repo is already registered (`{resolved.repository_id}`)."
                )
            if not resolved.install_url:
                return CommandResult.error(
                    "Server didn't return an install URL — `github_app_slug` may be unset."
                )
            _open_in_browser(resolved.install_url)
            return CommandResult.markdown(
                f"### Install Ember CodeIndex\nOpening your browser:\n`{resolved.install_url}`"
            )

        if subcommand == "status":
            local_sha = sync.current_sha()
            last = sync.last_synced_sha
            head = index.head()
            remote_url = sync.resolver.remote_url() if sync.resolver else None
            resolved = sync.resolver.cached if sync.resolver else None
            lines = "## CodeIndex Status\n"
            lines += f"- local HEAD: `{local_sha or 'not a git repo'}`\n"
            lines += f"- git remote: `{remote_url or 'not a git repo'}`\n"
            lines += f"- last synced: `{last or 'never'}`\n"
            lines += f"- index head: `{head or 'none'}`\n"
            if resolved is None:
                lines += "- discovered: `not yet (run /codeindex sync)`\n"
            elif resolved.needs_install:
                lines += "- discovered: `install required`\n"
                lines += f"- install URL: `{resolved.install_url or 'unavailable'}`\n"
            else:
                lines += f"- discovered: `{resolved.repository_id}`\n"
            return CommandResult.markdown(lines)

        return CommandResult.markdown(
            "## CodeIndex\n"
            "Run `/codeindex` with no args to open the interactive status "
            "panel (current-commit indexed state + sync/clean/install "
            "actions, with a 2s live poll).\n"
            "- `/codeindex search <query>` — semantic search the head commit (chat output)\n"
            "- `/codeindex item <id>` — show full item details in chat\n"
            "- `/codeindex commits` — list indexed commits as markdown\n"
            "- `/codeindex clean` — drop stale, non-branch commits\n"
            "- `/codeindex sync [sha]` — pull and apply a changeset (defaults to HEAD)\n"
            "- `/codeindex resync [sha]` — wipe local state and pull a fresh snapshot\n"
            "- `/codeindex install` — open the GitHub App install page for this repo\n"
            "- `/codeindex status` — show sync state and install progress\n"
        )

    async def _cmd_model(self, args: str) -> "CommandResult":
        name = args.strip()
        if name:
            # Direct switch: /model gemini-2.5-flash
            registry = self._session.settings.models.registry
            if name not in registry:
                available = ", ".join(sorted(registry.keys()))
                return CommandResult.error(f"Unknown model: '{name}'. Available: {available}")
            self._session.settings.models.default = name
            self._session.main_team = self._session._build_main_agent()
            # ``action="model_switched"`` tells the FE to refresh the
            # status-bar model slot. Without it the bar showed the
            # OLD model after ``/model <name>`` direct switches —
            # nothing else triggers a refresh on that code path.
            return CommandResult(
                kind=CommandResultKind.INFO,
                content=f"Switched to model: {name}",
                action=CommandAction.MODEL_SWITCHED,
            )
        # No args: show picker
        return CommandResult.model()

    async def _cmd_config(self, _args: str) -> "CommandResult":
        s = self._session.settings

        # Auth status line
        from ember_code.core.auth.credentials import is_token_expired, load_credentials

        creds = load_credentials()
        if creds and not is_token_expired(creds):
            auth_status = creds.email or "logged in"
        else:
            auth_status = "not logged in"

        return CommandResult.markdown(
            "## Configuration\n"
            f"- **Model:** {s.models.default}\n"
            f"- **Auth:** {auth_status}\n"
            f"- **Permissions:** file_write={s.permissions.file_write}, "
            f"shell={s.permissions.shell_execute}\n"
            f"- **Storage:** {s.storage.backend}\n"
            f"- **Learning:** {'enabled' if s.learning.enabled else 'disabled'}\n"
            f"- **Reasoning tools:** {'enabled' if s.reasoning.enabled else 'disabled'}\n"
            f"- **Guardrails:** "
            f"{'PII ' if s.guardrails.pii_detection else ''}"
            f"{'injection ' if s.guardrails.prompt_injection else ''}"
            f"{'moderation ' if s.guardrails.moderation else ''}"
            f"{'(none)' if not any([s.guardrails.pii_detection, s.guardrails.prompt_injection, s.guardrails.moderation]) else ''}\n"
            f"- **Knowledge:** {'enabled' if s.knowledge.enabled else 'disabled'}\n"
            f"- **Compression:** enabled\n"
            f"- **Session summaries:** enabled\n"
            f"- **Max agents:** {s.orchestration.max_total_agents}\n"
            f"- **Max depth:** {s.orchestration.max_nesting_depth}\n"
            f"- **Session:** {self._session.session_id}\n"
        )

    async def _cmd_mcp(self, _args: str) -> "CommandResult":
        return CommandResult.mcp()

    async def _cmd_login(self, _args: str) -> "CommandResult":
        return CommandResult.login()

    async def _cmd_logout(self, _args: str) -> "CommandResult":
        from ember_code.core.auth.credentials import clear_credentials, load_credentials

        creds = load_credentials()
        clear_credentials()

        # Clear in-memory cloud state and rebuild agent with direct model URL
        messages = []
        if self._session:
            from ember_code.core.auth.credentials import CloudCredentials

            self._session._cloud = CloudCredentials(path="/dev/null")

            # If current model uses cloud_token, switch to a non-cloud model
            current = self._session.settings.models.default
            registry = self._session.settings.models.registry
            current_cfg = registry.get(current, {})
            if current_cfg.get("api_key") == "cloud_token":
                # Find first model with its own credentials
                fallback = next(
                    (
                        name
                        for name, cfg in registry.items()
                        if cfg.get("api_key") and cfg.get("api_key") != "cloud_token"
                    ),
                    None,
                )
                if fallback:
                    self._session.settings.models.default = fallback
                    messages.append(f"Switched to {fallback} (cloud model no longer available).")
                else:
                    messages.append(
                        "Warning: no models with API keys configured. "
                        "Add a model with an api_key or /login again."
                    )

            self._session.main_team = self._session._build_main_agent()

        email_msg = f"Logged out ({creds.email})." if creds else "Not logged in."
        messages.insert(0, email_msg)
        return CommandResult(
            kind=CommandResultKind.INFO,
            content="\n".join(messages),
            action=CommandAction.LOGOUT,
        )

    async def _cmd_whoami(self, _args: str) -> "CommandResult":
        from ember_code.core.auth.credentials import is_token_expired, load_credentials

        creds = load_credentials()
        if creds is None:
            return CommandResult.info("Not logged in. Use /login to authenticate.")
        if is_token_expired(creds):
            return CommandResult.info(
                f"Session expired for {creds.email}. Use /login to re-authenticate."
            )
        expires = creds.expires_at[:19] if creds.expires_at else "unknown"
        return CommandResult.info(f"Logged in as {creds.email} (expires: {expires})")

    async def _cmd_schedule(self, args: str) -> "CommandResult":
        """Handle /schedule commands: add, list, remove, show."""

        from ember_code.core.scheduler.models import TaskStatus
        from ember_code.core.scheduler.store import TaskStore

        store = TaskStore()
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else "list"
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        # No args or "list" → open the task panel
        if subcommand == "list" or not args.strip():
            return CommandResult(
                kind=CommandResultKind.INFO, content="", action=CommandAction.SCHEDULE
            )

        if subcommand == "add" and sub_args:
            return await self._schedule_add(store, sub_args)

        if subcommand in ("rm", "remove", "cancel") and sub_args:
            task_id = sub_args.strip()
            task = await store.get(task_id)
            if not task:
                return CommandResult.error(f"Task not found: {task_id}")
            if task.status in (TaskStatus.pending, TaskStatus.running):
                await store.update_status(task_id, TaskStatus.cancelled)
                return CommandResult.info(f"Cancelled task {task_id}")
            return CommandResult.info(f"Task {task_id} is already {task.status.value}")

        if subcommand == "show" and sub_args:
            task = await store.get(sub_args.strip())
            if not task:
                return CommandResult.error(f"Task not found: {sub_args.strip()}")
            lines = (
                f"## Task {task.id}\n"
                f"- **Description:** {task.description}\n"
                f"- **Scheduled:** {task.scheduled_at.strftime('%Y-%m-%d %H:%M')}\n"
                f"- **Status:** {task.status.value}\n"
                f"- **Created:** {task.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            )
            if task.result:
                lines += f"\n**Result:**\n{task.result}\n"
            if task.error:
                lines += f"\n**Error:**\n{task.error}\n"
            return CommandResult.markdown(lines)

        # Unknown subcommand — open the panel
        return CommandResult(kind=CommandResultKind.INFO, content="", action=CommandAction.SCHEDULE)

    @staticmethod
    async def _schedule_add(store, text: str) -> "CommandResult":
        """Parse 'description at/in/every time' and create a task."""
        import uuid

        from ember_code.core.scheduler.models import ScheduledTask
        from ember_code.core.scheduler.parser import parse_recurrence, parse_time

        # Try recurring: "run tests every 2 hours", "check deps daily", "audit weekly at 9am"
        for sep in (" every ", " daily", " hourly", " weekly"):
            idx = text.lower().rfind(sep)
            if idx > 0:
                description = text[:idx].strip()
                recur_part = text[idx:].strip()
                recurrence, scheduled = parse_recurrence(recur_part)
                if recurrence and scheduled:
                    task = ScheduledTask(
                        id=uuid.uuid4().hex[:8],
                        description=description,
                        scheduled_at=scheduled,
                        recurrence=recurrence,
                    )
                    await store.add(task)
                    return CommandResult.info(
                        f'Scheduled `{task.id}`: "{description}" '
                        f"({recurrence}, first at {scheduled.strftime('%Y-%m-%d %H:%M')})"
                    )

        # Try one-shot: "review codebase at 5pm"
        for sep in (" at ", " in ", " on ", " tomorrow"):
            idx = text.lower().rfind(sep)
            if idx > 0:
                description = text[:idx].strip()
                time_part = text[idx:].strip()
                scheduled = parse_time(time_part)
                if scheduled:
                    task = ScheduledTask(
                        id=uuid.uuid4().hex[:8],
                        description=description,
                        scheduled_at=scheduled,
                    )
                    await store.add(task)
                    return CommandResult.info(
                        f'Scheduled `{task.id}`: "{description}" at {scheduled.strftime("%Y-%m-%d %H:%M")}'
                    )

        return CommandResult.error(
            "Could not parse time. Examples:\n"
            "  /schedule add review the codebase at 5pm\n"
            "  /schedule add run tests in 30 minutes\n"
            "  /schedule add audit security tomorrow\n"
            "  /schedule add run tests every 2 hours\n"
            "  /schedule add check dependencies daily"
        )

    async def _cmd_evals(self, args: str) -> "CommandResult":
        from ember_code.core.evals.reporter import format_results
        from ember_code.core.evals.runner import SuiteResult

        agent_filter = args.strip() or None

        results = await SuiteResult.run_all(
            pool=self._session.pool,
            settings=self._session.settings,
            project_dir=self._session.project_dir,
            agent_filter=agent_filter,
        )

        if not results:
            return CommandResult.info("No eval suites found. Add YAML files to .ember/evals/")
        return CommandResult.markdown(format_results(results))

    async def _cmd_sync_knowledge(self, _args: str) -> "CommandResult":
        if not self._session.knowledge_mgr.share_enabled():
            return CommandResult.info(
                "Knowledge sharing is not enabled. Set knowledge.share=true in config."
            )
        results = await self._session.knowledge_mgr.sync_bidirectional()
        lines = [f"[{r.direction}] {r.summary}" for r in results]
        return CommandResult.info("\n".join(lines))

    async def _cmd_compact(self, _args: str) -> "CommandResult":
        status, summary = await self._session.force_compact()
        return CommandResult(
            kind=CommandResultKind.ACTION,
            action=CommandAction.COMPACT,
            content=summary or status,
        )

    # ── /loop ─────────────────────────────────────────────────────────
    # In-session loop primitive. After the user invokes ``/loop <prompt>``,
    # the same prompt is automatically re-fired as the next user turn
    # once the previous one completes. Stops on ``/loop stop``, any
    # non-``/loop`` user input, or when the iteration cap is hit.
    # Mirrors ``/schedule``'s shape but with a zero-gap continuation
    # instead of a cron schedule — runs live in the user's TUI so each
    # iteration streams normally.

    # Defined in ``core/loop/limits.py`` so the slash command and the
    # agent tool stay in lockstep. Kept as instance attributes so the
    # call sites read like the legacy ``self._LOOP_*`` references.
    from ember_code.core.loop.limits import (
        LOOP_DEFAULT_MAX_ITERATIONS as _LOOP_DEFAULT_MAX_ITERATIONS,
    )
    from ember_code.core.loop.limits import (
        LOOP_HARD_CAP as _LOOP_HARD_CAP,
    )

    async def _cmd_loop(self, args: str) -> "CommandResult":
        """Drive a prompt in a loop within the current session.

        Subcommands:
          - ``/loop`` → show status / help.
          - ``/loop stop`` → cancel the active loop.
          - ``/loop <prompt>`` → start, with default cap (30 iterations).
          - ``/loop <N> <prompt>`` → start with explicit cap N.
        """
        text = args.strip()

        # No args → open the TUI panel (the panel polls
        # ``loop_status`` live, replacing the old static chat
        # snapshot). ``_loop_status`` is kept on the handler for
        # any future scripted callers, but the slash command no
        # longer reaches it.
        if not text:
            return CommandResult.loop()

        # Stop.
        if text.lower() in {"stop", "cancel", "off", "end"}:
            return await self._loop_stop()

        # Resume an interrupted (paused) loop. The session was killed
        # mid-iteration; the persisted state was loaded on startup but
        # nothing is firing yet. ``resume_loop`` flips the paused
        # flag and returns the prompt, which we send back as a
        # ``run_prompt`` action — the FE fires ``_run(prompt)``
        # directly, re-running the interrupted iteration. After that
        # finishes, the normal post-run continuation hook takes over.
        if text.lower() == "resume":
            sess = self._session
            if sess.pending_loop_prompt is None:
                return CommandResult.error("No loop to resume.")
            if not sess.loop_paused:
                return CommandResult.info("Loop is already running — no resume needed.")
            prompt = await sess.resume_loop()
            if prompt is None:
                # Race: another caller flipped paused False between the
                # checks above and the resume call. Treat as already
                # running.
                return CommandResult.info("Loop is already running.")
            return CommandResult(
                kind=CommandResultKind.INFO,
                content=prompt,
                action=CommandAction.RUN_PROMPT,
            )

        # Parse leading "<N>" or "<N>x" as the iteration cap.
        # ``cap_explicit`` tracks whether the user supplied a number
        # at all — explicit means "exactly N", implicit means
        # "default safety net of LOOP_DEFAULT_MAX_ITERATIONS that
        # auto-extends past on cap-hit". The two semantics diverge
        # inside ``Session.advance_loop``.
        max_iter = self._LOOP_DEFAULT_MAX_ITERATIONS
        cap_explicit = False
        first, _, rest = text.partition(" ")
        first_num = first.rstrip("x")
        if first_num.isdigit():
            n = int(first_num)
            if n <= 0:
                return CommandResult.error(
                    "Iteration cap must be positive. Try `/loop 5 your prompt`."
                )
            if n > self._LOOP_HARD_CAP:
                return CommandResult.error(
                    f"Iteration cap {n} exceeds the hard cap of "
                    f"{self._LOOP_HARD_CAP}. Pick a smaller number."
                )
            max_iter = n
            cap_explicit = True
            prompt = rest.strip()
        else:
            prompt = text

        if not prompt:
            return CommandResult.error(
                "Loop needs a prompt. Try `/loop fix the typo in foo.py, bar.py`."
            )

        # Refuse to start a second loop on top of an active one — the
        # user almost certainly wants to /loop stop first.
        sess = self._session
        if sess.pending_loop_prompt is not None:
            return CommandResult.error(
                f"A loop is already running ({sess.loop_iteration_index} done, "
                f"{sess.loop_iterations_remaining} remaining). "
                "Run `/loop stop` first, then start a new one."
            )

        # Slash-command path: iteration 1 fires immediately via the
        # ``run_prompt`` action below, so we use ``immediate=True``
        # which initializes ``index=1, remaining=max-1``. Subsequent
        # iterations are driven by ``_check_loop_continuation`` →
        # ``advance_loop`` in the run controller.
        await sess.start_loop(prompt, max_iter, immediate=True, cap_explicit=cap_explicit)

        # Wrap iteration 1's prompt with the autonomous-loop
        # meta-instruction so the agent doesn't ask the user
        # questions between iterations. Iterations 2+ get wrapped
        # inside ``Session.advance_loop``; this branch handles
        # iteration 1 because the slash command fires it directly
        # via ``run_prompt`` rather than going through
        # ``advance_loop``. ``display_content`` carries the bare
        # prompt for chat rendering — the wrapper is only meant
        # for the agent. ``total`` is only included in the wrapper
        # when the user explicitly capped the run.
        from ember_code.core.loop import wrap_iteration_prompt

        wrapped = wrap_iteration_prompt(
            prompt, iteration=1, total=max_iter if cap_explicit else None
        )

        return CommandResult(
            kind=CommandResultKind.INFO,
            content=wrapped,
            display_content=prompt,
            action=CommandAction.RUN_PROMPT,
        )

    def _loop_status(self) -> "CommandResult":
        sess = self._session
        if sess.pending_loop_prompt is None:
            return CommandResult.info(
                "No loop is running.\n\n"
                "Usage:\n"
                "  /loop <prompt>          start (default cap: 30 iterations)\n"
                "  /loop <N> <prompt>      start with explicit cap N\n"
                "  /loop stop              cancel the active loop\n"
                "  /loop resume            re-fire the interrupted iteration after a restart"
            )
        return CommandResult.info(
            f"Loop active: {sess.loop_iteration_index} done, "
            f"{sess.loop_iterations_remaining} remaining.\n"
            f"Prompt: {sess.pending_loop_prompt!r}"
        )

    async def _loop_stop(self) -> "CommandResult":
        sess = self._session
        if sess.pending_loop_prompt is None:
            return CommandResult.info("No loop is running.")
        done = sess.loop_iteration_index
        await sess.cancel_loop()
        return CommandResult.info(f"Loop stopped after {done} iteration{'s' if done != 1 else ''}.")

    async def _cmd_bug(self, _args: str) -> "CommandResult":
        import webbrowser

        url = "https://github.com/vector-bridge/ember__code/issues"
        webbrowser.open(url)
        return CommandResult.info(f"Opened {url}")

    async def _handle_skill(self, stripped: str) -> "CommandResult":
        """Try to match and execute a skill command.

        Instead of running non-interactively, we feed the rendered skill
        prompt into the main agent's streaming run loop so the user sees
        tool calls, progress, and streaming output.
        """
        skill_match = self._session.skill_pool.match_user_command(stripped)
        if skill_match:
            skill, args = skill_match
            rendered = skill.render(args, session_id=self._session.session_id)
            return CommandResult(
                kind=CommandResultKind.INFO,
                content=rendered,
                action=CommandAction.RUN_PROMPT,
            )
        return CommandResult.error(f"Unknown command: {stripped.split()[0]}")

    # ── Command dispatch table ────────────────────────────────────

    _COMMANDS: dict[str, Any] = {
        "/quit": _cmd_quit,
        "/exit": _cmd_quit,
        "/help": _cmd_help,
        "/agents": _cmd_agents,
        "/skills": _cmd_skills,
        "/hooks": _cmd_hooks,
        "/clear": _cmd_clear,
        "/sessions": _cmd_sessions,
        "/rename": _cmd_rename,
        "/memory": _cmd_memory,
        "/knowledge": _cmd_knowledge,
        "/codeindex": _cmd_codeindex,
        "/config": _cmd_config,
        "/model": _cmd_model,
        "/mcp": _cmd_mcp,
        "/login": _cmd_login,
        "/logout": _cmd_logout,
        "/whoami": _cmd_whoami,
        "/schedule": _cmd_schedule,
        "/loop": _cmd_loop,
        "/plugin": _cmd_plugin,
        "/plugins": _cmd_plugins,
        "/compact": _cmd_compact,
        "/bug": _cmd_bug,
        "/evals": _cmd_evals,
        "/sync-knowledge": _cmd_sync_knowledge,
    }
