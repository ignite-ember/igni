"""Tiny coordinators for action-only + trivial slash commands.

Extracted from :mod:`ember_code.backend.command_handler` — each
of the commands below had a 1–3 line inline body in the god-file
that just returned a ``CommandResult.for_action(...)`` (open a
panel) or opened an issue-tracker URL. Consolidated here as
class-per-command coordinators so the file count stays sane
while keeping the OOP posture (methods on classes, no free
functions with state as first arg).

* :class:`BugCommand` — ``/bug``: open the GitHub issue tracker.
* :class:`QuitCommand` — ``/quit`` and ``/exit``: session exit.
* :class:`WatcherCommand` — ``/watcher``: open the background-
  process watcher panel.
* :class:`SkillsCommand` — ``/skills``: open the skills panel.
* :class:`McpCommand` — ``/mcp``: open the MCP servers panel.

All use :class:`BrowserOpener` for URL-opening (single spot for
"best-effort open + swallow errors") so the two previous copies
of ``_open_in_browser`` collapse to one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.browser_opener import BrowserOpener
from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler


class BugCommand:
    """``/bug`` — open the issue tracker in the user's browser."""

    BUG_URL: str = "https://github.com/ignite-ember/igni/issues"

    def open(self) -> CommandResult:
        BrowserOpener.open(self.BUG_URL)
        return CommandResult.info(f"Opened {self.BUG_URL}")


class QuitCommand:
    """``/quit`` and ``/exit`` — terminate the session."""

    def quit(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.QUIT)


class WatcherCommand:
    """``/watcher`` — open the background-process watcher panel."""

    def open_panel(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.WATCHER)


class SkillsCommand:
    """``/skills`` — open the skills TUI panel.

    The panel surfaces description, version, source dir, argument
    hint, and an expandable preview of the skill body — strictly
    more information than the old markdown listing. Legacy
    markdown-dump form is gone; consumers that want a text dump
    should scrape the panel data via ``get_skill_details`` over
    RPC.
    """

    def open_panel(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.SKILLS)


class McpCommand:
    """``/mcp`` — open the MCP servers panel."""

    def open_panel(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.MCP)


# ── Public shims ─────────────────────────────────────────────────


async def cmd_bug(_handler: CommandHandler, _args: str) -> CommandResult:
    """Two-line shim for :class:`BugCommand`."""
    return BugCommand().open()


async def cmd_quit(_handler: CommandHandler, _args: str) -> CommandResult:
    """Two-line shim for :class:`QuitCommand`."""
    return QuitCommand().quit()


async def cmd_watcher(_handler: CommandHandler, _args: str) -> CommandResult:
    """Two-line shim for :class:`WatcherCommand`."""
    return WatcherCommand().open_panel()


async def cmd_skills(_handler: CommandHandler, _args: str) -> CommandResult:
    """Two-line shim for :class:`SkillsCommand`."""
    return SkillsCommand().open_panel()


async def cmd_mcp(_handler: CommandHandler, _args: str) -> CommandResult:
    """Two-line shim for :class:`McpCommand`."""
    return McpCommand().open_panel()
