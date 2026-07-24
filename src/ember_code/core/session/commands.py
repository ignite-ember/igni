"""Slash command dispatch for the --no-tui interactive session loop.

:class:`InteractiveCommandDispatcher` wraps the shared
:class:`CommandHandler` and routes each :class:`CommandResult` through
a per-action method table. TUI-only actions (sessions picker, model
picker, MCP panel, login widget) get plain-text equivalents.
"""

from __future__ import annotations

from typing import ClassVar

from ember_code.backend.command_handler import CommandHandler, CommandResult
from ember_code.core.session.core import Session
from ember_code.core.session.schemas import McpServerStatus
from ember_code.protocol.messages import CommandAction, CommandResultKind


class InteractiveCommandDispatcher:
    """Runs slash commands for the plain-text (--no-tui) REPL.

    Wraps the shared :class:`CommandHandler` and routes each
    :class:`CommandResult` through a per-action method table
    (see :attr:`_ACTION_HANDLERS`). Actions that only exist as
    interactive widgets in the TUI (sessions picker, model
    picker, MCP panel, login) get plain-text equivalents here.
    """

    # Action → method-name map. Resolved via ``getattr(self, name)``
    # inside :meth:`_render` so entries dispatch to bound methods.
    _ACTION_HANDLERS: ClassVar[dict[CommandAction, str]] = {
        CommandAction.QUIT: "_render_quit",
        CommandAction.CLEAR: "_render_clear",
        CommandAction.SESSIONS: "_render_sessions",
        CommandAction.MODEL: "_render_model_picker",
        CommandAction.MCP: "_render_mcp_status",
        CommandAction.COMPACT: "_render_compact",
        CommandAction.LOGIN: "_render_login",
    }

    def __init__(self, session: Session) -> None:
        self.session = session

    async def dispatch(self, command: str) -> bool:
        """Dispatch a slash command. Returns True if handled, False if unknown."""
        handler = CommandHandler(self.session)
        result = await handler.handle(command.strip())

        # Unknown command — let the caller handle it (skill matching, etc.)
        if result.kind == CommandResultKind.ERROR and "Unknown command" in result.content:
            return False

        await self._render(result)
        return True

    async def _render(self, result: CommandResult) -> None:
        """Render a CommandResult as plain text output."""
        method_name = self._ACTION_HANDLERS.get(result.action)
        if method_name is not None:
            await getattr(self, method_name)(result)
            return
        # No dedicated action handler — fall through to the kind-based
        # renderers (MARKDOWN / INFO / ERROR). A ``CommandAction`` added
        # later without a matching entry lands here by design.
        self._render_by_kind(result)

    # ── Per-action handlers ─────────────────────────────────────

    async def _render_quit(self, result: CommandResult) -> None:
        raise SystemExit(0)

    async def _render_clear(self, result: CommandResult) -> None:
        self.session.display.print_info(
            f"Conversation cleared. New session: {self.session.session_id}"
        )

    async def _render_compact(self, result: CommandResult) -> None:
        display = self.session.display
        display.print_info("Context compacted. Old messages summarized and cleared.")
        if result.content:
            display.print_info(f"Summary:\n{result.content}")

    async def _render_login(self, result: CommandResult) -> None:
        self.session.display.print_info(
            "Login is only available in TUI mode. Run without --no-tui to use /login."
        )

    async def _render_sessions(self, result: CommandResult) -> None:
        """List past sessions as text (no interactive picker)."""
        display = self.session.display
        sessions = await self.session.persistence.list_sessions()
        if not sessions:
            display.print_info("No past sessions found.")
            return
        lines = ["## Sessions"]
        for s in sessions[:20]:
            label = s.get("name") or s.get("session_id") or ""
            lines.append(f"- {label}")
        lines.append("\n[dim]Use --resume <id> to resume a session.[/dim]")
        display.print_markdown("\n".join(lines))

    async def _render_model_picker(self, result: CommandResult) -> None:
        """List available models as text (no interactive picker).

        Refreshes the cloud-discovered entries first so a key added on
        the portal shows up without restarting the CLI.
        """
        self.session.refresh_cloud_models()
        registry = self.session.settings.models.registry
        current = self.session.settings.models.default
        lines = ["## Models"]
        for name in sorted(registry.keys()):
            marker = " (current)" if name == current else ""
            lines.append(f"- {name}{marker}")
        lines.append("\n[dim]Use /model <name> to switch.[/dim]")
        self.session.display.print_markdown("\n".join(lines))

    async def _render_mcp_status(self, result: CommandResult) -> None:
        """Show MCP server status as text (no interactive panel)."""
        display = self.session.display
        mgr = self.session.mcp_manager
        servers = mgr.list_servers()
        if not servers:
            display.print_info("No MCP servers configured.")
            return
        connected = set(mgr.list_connected())
        statuses = [McpServerStatus(name=name, connected=name in connected) for name in servers]
        # Session-scoped failure cache populated by
        # :class:`~ember_code.core.session.startup.mcp.McpInitPhase`
        # and every controller connect / disconnect. Reading from
        # it (rather than re-calling ``mgr.connect``) avoids
        # firing a fresh subprocess just to render status.
        failures = self.session.mcp_failures
        lines = ["## MCP Servers"]
        for status in statuses:
            if status.connected:
                tools = mgr.get_tools(status.name)
                lines.append(f"- ● {status.name} — connected ({len(tools)} tools)")
            else:
                error = failures.get(status.name, "")
                detail = f"error: {error}" if error else "disconnected"
                lines.append(f"- ○ {status.name} — {detail}")
        display.print_markdown("\n".join(lines))

    # ── Kind-based fallback ─────────────────────────────────────

    def _render_by_kind(self, result: CommandResult) -> None:
        """Fallback renderer for results without a dedicated action handler."""
        display = self.session.display
        if result.kind == CommandResultKind.MARKDOWN:
            display.print_markdown(result.content)
        elif result.kind == CommandResultKind.INFO:
            display.print_info(result.content)
        elif result.kind == CommandResultKind.ERROR:
            display.print_error(result.content)
