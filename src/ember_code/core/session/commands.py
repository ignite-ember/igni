"""Slash command dispatch for the --no-tui interactive session loop.

Delegates to the shared ``CommandHandler`` (same one used by the TUI) so
that both modes always have the same command set.  TUI-only *actions*
(sessions picker, model picker, MCP panel, login widget) are replaced
with text-based equivalents.
"""

from __future__ import annotations

from ember_code.backend.command_handler import CommandHandler, CommandResult
from ember_code.core.session.core import Session
from ember_code.core.utils.display import print_error, print_info, print_markdown
from ember_code.protocol.messages import CommandAction, CommandResultKind


async def dispatch(session: Session, command: str) -> bool:
    """Dispatch a slash command. Returns True if handled, False if unknown."""
    handler = CommandHandler(session)
    result = await handler.handle(command.strip())

    # Unknown command — let the caller handle it (skill matching, etc.)
    if result.kind == CommandResultKind.ERROR and "Unknown command" in result.content:
        return False

    await _render_result(session, result)
    return True


async def _render_result(session: Session, result: CommandResult) -> None:
    """Render a CommandResult as plain text output."""
    action = result.action
    if action == CommandAction.QUIT:
        raise SystemExit(0)
    elif action == CommandAction.CLEAR:
        print_info(f"Conversation cleared. New session: {session.session_id}")
    elif action == CommandAction.SESSIONS:
        await _text_sessions(session)
    elif action == CommandAction.MODEL:
        _text_model_picker(session)
    elif action == CommandAction.MCP:
        _text_mcp_status(session)
    elif action == CommandAction.COMPACT:
        print_info("Context compacted. Old messages summarized and cleared.")
        if result.content:
            print_info(f"Summary:\n{result.content}")
    elif action == CommandAction.LOGIN:
        print_info("Login is only available in TUI mode. Run without --no-tui to use /login.")
    elif result.kind == CommandResultKind.MARKDOWN:
        print_markdown(result.content)
    elif result.kind == CommandResultKind.INFO:
        print_info(result.content)
    elif result.kind == CommandResultKind.ERROR:
        print_error(result.content)


# ── Text fallbacks for TUI-only actions ──────────────────────────────


async def _text_sessions(session: Session) -> None:
    """List past sessions as text (no interactive picker)."""
    sessions = []
    if hasattr(session.persistence, "list_sessions"):
        sessions = await session.persistence.list_sessions()
    if not sessions:
        print_info("No past sessions found.")
        return
    lines = ["## Sessions"]
    for s in sessions[:20]:
        name = getattr(s, "name", "") or getattr(s, "session_id", str(s))
        lines.append(f"- {name}")
    lines.append("\n[dim]Use --resume <id> to resume a session.[/dim]")
    print_markdown("\n".join(lines))


def _text_model_picker(session: Session) -> None:
    """List available models as text (no interactive picker).

    Refreshes the cloud-discovered entries first so a key added on the
    portal shows up without restarting the CLI. The refresh is bounded
    by ``cloud_models._FETCH_TIMEOUT_SECONDS`` and silently degrades
    to whatever's already in the registry on any failure.
    """
    session.refresh_cloud_models()
    registry = session.settings.models.registry
    current = session.settings.models.default
    lines = ["## Models"]
    for name in sorted(registry.keys()):
        marker = " (current)" if name == current else ""
        lines.append(f"- {name}{marker}")
    lines.append("\n[dim]Use /model <name> to switch.[/dim]")
    print_markdown("\n".join(lines))


def _text_mcp_status(session: Session) -> None:
    """Show MCP server status as text (no interactive panel)."""
    mgr = session.mcp_manager
    servers = mgr.list_servers()
    if not servers:
        print_info("No MCP servers configured.")
        return
    connected = set(mgr.list_connected())
    lines = ["## MCP Servers"]
    for name in servers:
        if name in connected:
            tools = mgr.get_tools(name)
            lines.append(f"- ● {name} — connected ({len(tools)} tools)")
        else:
            error = mgr.get_error(name)
            status = f"error: {error}" if error else "disconnected"
            lines.append(f"- ○ {name} — {status}")
    print_markdown("\n".join(lines))
