"""Registry: single source of truth for slash-command → handler wiring.

Extracted from :mod:`ember_code.backend.command_handler` — the
old god-file kept three parallel structures for every built-in
command:

  1. ``BUILTIN_DESCRIPTIONS`` — a dict of ``name → description``
     consumed by :class:`SlashCommandsCatalog`.
  2. ``_COMMANDS`` — a dict of ``"/name" → unbound method``
     used for dispatch.
  3. A one-line ``async def _cmd_x(self, args)`` delegate method
     on :class:`CommandHandler` for each command, forwarding to
     the sibling module's ``cmd_x`` shim.

Adding a new command required three edits across two dicts + a
method. :class:`BuiltinCommandRegistry` collapses all three:
each :class:`BuiltinCommand` entry points directly at the sibling
coordinator's ``cmd_xxx`` shim, and both the descriptions catalog
and dispatch table read from the same list.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.backend.command_result import CommandResult


# All builtin shim callables share this signature: ``(handler, args)``
# → ``Awaitable[CommandResult]``. A command whose shim takes only the
# handler is registered with ``takes_args=False`` and the registry
# drops the args for it.
BuiltinHandler = Callable[["CommandHandler", str], Awaitable["CommandResult"]]
NoArgsHandler = Callable[["CommandHandler"], Awaitable["CommandResult"]]


@dataclass(frozen=True)
class BuiltinCommand:
    """One entry in the registry: slash-key + description + where its
    shim lives.

    ``target`` is ``"module:attr"``. The module is imported the first
    time the command runs, never before: an idle backend carries
    none of what ``/evals``, ``/loop``, ``/schedule`` or ``/plugin``
    need (sqlalchemy, alembic, dateparser, the evals runner…). Listing
    and describing commands reads only this entry.
    """

    slash: str  # e.g. "/agents"
    description: str
    target: str  # e.g. "ember_code.backend.cmd_agents:cmd_agents"
    takes_args: bool = True
    _resolved: list[BuiltinHandler] = field(
        default_factory=list, repr=False, compare=False, hash=False
    )

    @property
    def bare(self) -> str:
        """Name with the leading slash stripped."""
        return self.slash.lstrip("/")

    @property
    def module(self) -> str:
        """Dotted path of the module the shim lives in."""
        return self.target.partition(":")[0]

    def resolve(self) -> BuiltinHandler:
        """The shim, imported on first use and kept.

        This is the one place in the backend that imports by name at
        run time — deliberately, so the commands' dependencies load
        when somebody runs one rather than when the backend starts.
        """
        if not self._resolved:
            module_name, _, attr = self.target.partition(":")
            shim = getattr(importlib.import_module(module_name), attr)
            if self.takes_args:
                self._resolved.append(cast(BuiltinHandler, shim))
            else:
                no_args = cast(NoArgsHandler, shim)

                async def _without_args(handler: CommandHandler, _args: str) -> CommandResult:
                    return await no_args(handler)

                self._resolved.append(_without_args)
        return self._resolved[0]


_B = "ember_code.backend"


class BuiltinCommandRegistry:
    """Registry of every built-in slash command.

    Adding a new builtin means one entry in :meth:`_build_entries` —
    the descriptions catalog and dispatch table are both derived
    from that single list, and nothing behind an entry is imported
    until the command runs.
    """

    def __init__(self) -> None:
        self._entries: tuple[BuiltinCommand, ...] = tuple(self._build_entries())
        self._by_slash: Mapping[str, BuiltinCommand] = MappingProxyType(
            {e.slash: e for e in self._entries}
        )
        self._by_bare: Mapping[str, BuiltinCommand] = MappingProxyType(
            {e.bare: e for e in self._entries}
        )

    @staticmethod
    def _build_entries() -> list[BuiltinCommand]:
        return [
            BuiltinCommand("/quit", "Exit the session", f"{_B}.cmd_misc:cmd_quit"),
            BuiltinCommand("/exit", "Exit the session", f"{_B}.cmd_misc:cmd_quit"),
            BuiltinCommand("/help", "Show help and available commands", f"{_B}.cmd_help:cmd_help"),
            BuiltinCommand(
                "/watcher",
                "Open the background-process watcher panel",
                f"{_B}.cmd_misc:cmd_watcher",
            ),
            BuiltinCommand("/agents", "List and manage agents", f"{_B}.cmd_agents:cmd_agents"),
            BuiltinCommand("/skills", "List installed skills", f"{_B}.cmd_misc:cmd_skills"),
            BuiltinCommand("/hooks", "List installed hooks", f"{_B}.cmd_hooks:cmd_hooks"),
            BuiltinCommand(
                "/eject",
                "Copy something your group ships into this project, so yours wins",
                f"{_B}.cmd_eject:cmd_eject",
            ),
            BuiltinCommand(
                "/clear",
                "Clear the current conversation",
                f"{_B}.cmd_session:cmd_clear",
                takes_args=False,
            ),
            BuiltinCommand(
                "/sessions",
                "List past sessions",
                f"{_B}.cmd_session:cmd_sessions",
                takes_args=False,
            ),
            BuiltinCommand("/rename", "Rename the current session", f"{_B}.cmd_session:cmd_rename"),
            BuiltinCommand(
                "/fork", "Fork the current session under a new id", f"{_B}.cmd_session:cmd_fork"
            ),
            BuiltinCommand(
                "/memory", "Inspect or optimise learned memories", f"{_B}.cmd_memory:cmd_memory"
            ),
            BuiltinCommand("/config", "Show current settings", f"{_B}.cmd_config:cmd_config"),
            BuiltinCommand("/model", "Switch the active model", f"{_B}.cmd_model:cmd_model"),
            BuiltinCommand("/mcp", "Open the MCP servers panel", f"{_B}.cmd_misc:cmd_mcp"),
            BuiltinCommand(
                "/login", "Sign in to igni Cloud", f"{_B}.cmd_auth:cmd_login", takes_args=False
            ),
            BuiltinCommand(
                "/logout", "Sign out of igni Cloud", f"{_B}.cmd_auth:cmd_logout", takes_args=False
            ),
            BuiltinCommand(
                "/whoami",
                "Show the signed-in user",
                f"{_B}.cmd_auth:cmd_whoami",
                takes_args=False,
            ),
            BuiltinCommand(
                "/schedule",
                "Schedule one-shot or recurring tasks",
                f"{_B}.cmd_schedule:cmd_schedule",
            ),
            BuiltinCommand("/loop", "Run a prompt in a loop", f"{_B}.cmd_loop:cmd_loop"),
            BuiltinCommand(
                "/plugin", "Install / update / remove a plugin", f"{_B}.cmd_plugin:cmd_plugin"
            ),
            BuiltinCommand("/plugins", "Open the plugins panel", f"{_B}.cmd_plugin:cmd_plugins"),
            BuiltinCommand(
                "/extensions",
                "Installed extensions and their settings",
                f"{_B}.cmd_misc:cmd_extensions",
            ),
            BuiltinCommand(
                "/compact",
                "Compact the conversation context",
                f"{_B}.cmd_context:cmd_compact",
                takes_args=False,
            ),
            BuiltinCommand(
                "/ctx",
                "Show context window usage",
                f"{_B}.cmd_context:cmd_ctx",
                takes_args=False,
            ),
            BuiltinCommand(
                "/plan",
                "Toggle plan mode (read-only sandbox + plan-then-execute workflow)",
                f"{_B}.cmd_modes:cmd_plan",
            ),
            BuiltinCommand(
                "/accept",
                "Toggle acceptEdits mode (auto-approve file edits)",
                f"{_B}.cmd_modes:cmd_accept",
            ),
            BuiltinCommand(
                "/bypass",
                "Toggle bypassPermissions mode (auto-approve every tool — no HITL prompts)",
                f"{_B}.cmd_modes:cmd_bypass",
            ),
            BuiltinCommand(
                "/output-style",
                "List or switch the active output style (tone / verbosity)",
                f"{_B}.cmd_context:cmd_output_style",
            ),
            BuiltinCommand("/bug", "Open a bug report", f"{_B}.cmd_misc:cmd_bug"),
            BuiltinCommand("/evals", "Run evaluation suites", f"{_B}.cmd_evals:cmd_evals"),
        ]

    # ── Public API ─────────────────────────────────────────────

    def get(self, slash_or_bare: str) -> BuiltinCommand | None:
        """Look up an entry by ``/name`` or bare ``name``."""
        if slash_or_bare.startswith("/"):
            return self._by_slash.get(slash_or_bare)
        return self._by_bare.get(slash_or_bare)

    def names(self) -> list[str]:
        """Bare (no-slash) names of every built-in command."""
        return [e.bare for e in self._entries]

    def entries(self) -> tuple[BuiltinCommand, ...]:
        """Every entry, in registration order."""
        return self._entries

    def describe(self, name: str) -> str:
        """One-liner description for a built-in command. Accepts
        either ``"help"`` or ``"/help"``; returns ``""`` for
        unknown commands so callers can safely list every builtin
        without breaking if a description entry is missing.
        """
        entry = self.get(name)
        return entry.description if entry is not None else ""

    async def dispatch(
        self, handler: CommandHandler, slash: str, args: str
    ) -> CommandResult | None:
        """Route ``slash`` to its shim, importing it on first use.
        Returns ``None`` when the command isn't a registered builtin
        so the caller can fall through to the next dispatch tier
        (markdown, skills).
        """
        entry = self._by_slash.get(slash)
        if entry is None:
            return None
        return await entry.resolve()(handler, args)


# Module-level singleton — entries are frozen and a resolved shim is
# the same for every caller, so one instance is safe to share.
BUILTIN_REGISTRY = BuiltinCommandRegistry()


__all__ = ["BuiltinCommand", "BuiltinCommandRegistry", "BUILTIN_REGISTRY"]
