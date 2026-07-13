"""Command handler — processes slash commands for the TUI."""

import logging
import webbrowser
from typing import TYPE_CHECKING, Any

from ember_code.core.auth.credentials import (
    is_token_expired,
    load_credentials,
)
from ember_code.core.config.permission_eval import PermissionMode
from ember_code.core.evals.reporter import format_results
from ember_code.core.evals.runner import SuiteResult

# Re-export the plugin-management symbols so existing test patches
# targeting ``ember_code.backend.command_handler.PluginInstaller`` /
# ``.add_marketplace`` etc. keep working after the plugin commands
# moved to ``cmd_plugin.py``. Same shim pattern used elsewhere in
# the codebase after a refactor.
from ember_code.core.plugins.installer import (  # noqa: F401
    PluginError,
    PluginInstaller,
)
from ember_code.core.plugins.marketplaces import (  # noqa: F401
    add_marketplace,
    load_registry,
    refresh_marketplace,
    remove_marketplace,
    resolve_install_ref,
)
from ember_code.core.utils.markdown_commands import discover_markdown_commands
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
    def fork(cls, new_session_id: str) -> "CommandResult":
        # The new session id rides in ``content`` so the FE has
        # everything it needs to switch + load history from one
        # round-trip.
        return cls(
            kind=CommandResultKind.ACTION,
            action=CommandAction.FORK,
            content=new_session_id,
        )

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

    @classmethod
    def watcher(cls) -> "CommandResult":
        return cls(kind=CommandResultKind.ACTION, action=CommandAction.WATCHER)


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

        # Try markdown-authored custom commands (CC parity:
        # ``.claude/commands/*.md``, ``.ember/commands/*.md`` at
        # user and project tiers). Falls through to skill matching
        # so user-authored Python skills still win over a same-name
        # markdown file — the user explicitly imported the skill,
        # whereas markdown files are best-effort drop-ins.
        md_result = await self._handle_markdown_command(cmd, args)
        if md_result is not None:
            return md_result

        # Try skill match
        return await self._handle_skill(stripped)

    # ── Commands ──────────────────────────────────────────────────

    async def _cmd_quit(self, _args: str) -> "CommandResult":
        return CommandResult.quit()

    async def _cmd_help(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_help.cmd_help`."""
        from ember_code.backend.cmd_help import cmd_help

        return await cmd_help(self, args)

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
        """See :func:`backend.cmd_plugin.cmd_plugin`."""
        from ember_code.backend.cmd_plugin import cmd_plugin

        return await cmd_plugin(self, args)

    async def _cmd_plugin_marketplace(
        self,
        rest: list[str],
        data_dir: str,
    ) -> "CommandResult":
        """See :func:`backend.cmd_plugin.cmd_plugin_marketplace`."""
        from ember_code.backend.cmd_plugin import cmd_plugin_marketplace

        return await cmd_plugin_marketplace(self, rest, data_dir)

    async def _cmd_plugins(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_plugin.cmd_plugins`."""
        from ember_code.backend.cmd_plugin import cmd_plugins

        return await cmd_plugins(self, args)

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

    async def _cmd_watcher(self, _args: str) -> "CommandResult":
        """Open the background-process watcher panel (right side
        of the chat). View-only tail with a kill button per
        process. The panel subscribes to the
        ``process_started`` / ``process_line`` / ``process_exited``
        push channels for real-time updates — no polling.
        """
        return CommandResult.watcher()

    async def _cmd_clear(self, _args: str) -> "CommandResult":
        """See :func:`backend.cmd_session.cmd_clear`."""
        from ember_code.backend.cmd_session import cmd_clear

        return await cmd_clear(self)

    async def _cmd_sessions(self, _args: str) -> "CommandResult":
        """See :func:`backend.cmd_session.cmd_sessions`."""
        from ember_code.backend.cmd_session import cmd_sessions

        return await cmd_sessions(self)

    async def _cmd_rename(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_session.cmd_rename`."""
        from ember_code.backend.cmd_session import cmd_rename

        return await cmd_rename(self, args)

    async def _cmd_fork(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_session.cmd_fork`."""
        from ember_code.backend.cmd_session import cmd_fork

        return await cmd_fork(self, args)


    async def _cmd_memory(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_memory.cmd_memory`."""
        from ember_code.backend.cmd_memory import cmd_memory

        return await cmd_memory(self, args)


    async def _cmd_knowledge(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_memory.cmd_knowledge`."""
        from ember_code.backend.cmd_memory import cmd_knowledge

        return await cmd_knowledge(self, args)


    async def _cmd_codeindex(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_codeindex.cmd_codeindex`."""
        from ember_code.backend.cmd_codeindex import cmd_codeindex

        return await cmd_codeindex(self, args)

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
        """See :func:`backend.cmd_auth.cmd_login`."""
        from ember_code.backend.cmd_auth import cmd_login

        return await cmd_login(self)


    async def _cmd_logout(self, _args: str) -> "CommandResult":
        """See :func:`backend.cmd_auth.cmd_logout`."""
        from ember_code.backend.cmd_auth import cmd_logout

        return await cmd_logout(self)


    async def _cmd_whoami(self, _args: str) -> "CommandResult":
        """See :func:`backend.cmd_auth.cmd_whoami`."""
        from ember_code.backend.cmd_auth import cmd_whoami

        return await cmd_whoami(self)


    async def _cmd_schedule(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_schedule.cmd_schedule`."""
        from ember_code.backend.cmd_schedule import cmd_schedule

        return await cmd_schedule(self, args)

    async def _cmd_evals(self, args: str) -> "CommandResult":
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
        """See :func:`backend.cmd_memory.cmd_sync_knowledge`."""
        from ember_code.backend.cmd_memory import cmd_sync_knowledge

        return await cmd_sync_knowledge(self)


    async def _cmd_ctx(self, _args: str) -> "CommandResult":
        """See :func:`backend.cmd_context.cmd_ctx`."""
        from ember_code.backend.cmd_context import cmd_ctx

        return await cmd_ctx(self)


    async def _cmd_plan(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_modes.cmd_plan`."""
        from ember_code.backend.cmd_modes import cmd_plan

        return await cmd_plan(self, args)

    async def _cmd_output_style(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_context.cmd_output_style`."""
        from ember_code.backend.cmd_context import cmd_output_style

        return await cmd_output_style(self, args)


    async def _cmd_accept(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_modes.cmd_accept`."""
        from ember_code.backend.cmd_modes import cmd_accept

        return await cmd_accept(self, args)

    async def _cmd_bypass(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_modes.cmd_bypass`."""
        from ember_code.backend.cmd_modes import cmd_bypass

        return await cmd_bypass(self, args)

    async def _cmd_compact(self, _args: str) -> "CommandResult":
        """See :func:`backend.cmd_context.cmd_compact`."""
        from ember_code.backend.cmd_context import cmd_compact

        return await cmd_compact(self)


    # ── /loop ─────────────────────────────────────────────────────────
    # In-session loop primitive. After the user invokes ``/loop <prompt>``,
    # the same prompt is automatically re-fired as the next user turn
    # once the previous one completes. Stops on ``/loop stop``, any
    # non-``/loop`` user input, or when the iteration cap is hit.
    # Mirrors ``/schedule``'s shape but with a zero-gap continuation
    # instead of a cron schedule — runs live in the user's TUI so each
    # iteration streams normally.

    async def _cmd_loop(self, args: str) -> "CommandResult":
        """See :func:`backend.cmd_loop.cmd_loop`."""
        from ember_code.backend.cmd_loop import cmd_loop

        return await cmd_loop(self, args)

    def _loop_status(self) -> "CommandResult":
        """See :func:`backend.cmd_loop.loop_status`."""
        from ember_code.backend.cmd_loop import loop_status

        return loop_status(self)

    async def _loop_stop(self) -> "CommandResult":
        """See :func:`backend.cmd_loop.loop_stop`."""
        from ember_code.backend.cmd_loop import loop_stop

        return await loop_stop(self)

    async def _cmd_bug(self, _args: str) -> "CommandResult":
        url = "https://github.com/ignite-ember/igni/issues"
        webbrowser.open(url)
        return CommandResult.info(f"Opened {url}")

    async def _handle_markdown_command(self, cmd: str, args: str) -> "CommandResult | None":
        """Look up a markdown-authored command by name and, if
        found, render its body into a prompt for the agent. Returns
        ``None`` to fall through to the next dispatch tier.

        Discovery happens per-invocation rather than at session
        init — the cost is a handful of stat() calls + a small YAML
        parse, dwarfed by the LLM round-trip that follows, and
        avoids stale caching when a user is iterating on a command
        file in another editor."""
        name = cmd.lstrip("/")
        if not name:
            return None
        try:
            read_claude = self._session.settings.rules.cross_tool_support
            commands = discover_markdown_commands(
                self._session.project_dir,
                read_claude=read_claude,
            )
        except Exception as exc:
            logger.debug("Markdown command discovery failed: %s", exc)
            return None
        md = commands.get(name)
        if md is None:
            return None
        try:
            rendered = await md.render(args, project_dir=self._session.project_dir)
        except Exception as exc:
            logger.warning("Markdown command /%s render failed: %s", name, exc)
            return CommandResult.error(f"/{name}: render failed: {exc}")
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=rendered,
            action=CommandAction.RUN_PROMPT,
        )

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
        "/watcher": _cmd_watcher,
        "/agents": _cmd_agents,
        "/skills": _cmd_skills,
        "/hooks": _cmd_hooks,
        "/clear": _cmd_clear,
        "/sessions": _cmd_sessions,
        "/rename": _cmd_rename,
        "/fork": _cmd_fork,
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
        "/ctx": _cmd_ctx,
        "/plan": _cmd_plan,
        "/accept": _cmd_accept,
        "/bypass": _cmd_bypass,
        "/output-style": _cmd_output_style,
        "/bug": _cmd_bug,
        "/evals": _cmd_evals,
        "/sync-knowledge": _cmd_sync_knowledge,
    }
