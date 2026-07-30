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

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from ember_code.backend.cmd_agents import cmd_agents
from ember_code.backend.cmd_auth import cmd_login, cmd_logout, cmd_whoami
from ember_code.backend.cmd_codeindex import cmd_codeindex
from ember_code.backend.cmd_config import cmd_config
from ember_code.backend.cmd_context import cmd_compact, cmd_ctx, cmd_output_style
from ember_code.backend.cmd_evals import cmd_evals
from ember_code.backend.cmd_help import cmd_help
from ember_code.backend.cmd_hooks import cmd_hooks
from ember_code.backend.cmd_knowledge import cmd_knowledge, cmd_sync_knowledge
from ember_code.backend.cmd_loop import cmd_loop
from ember_code.backend.cmd_memory import cmd_memory
from ember_code.backend.cmd_misc import (
    cmd_bug,
    cmd_mcp,
    cmd_quit,
    cmd_skills,
    cmd_watcher,
)
from ember_code.backend.cmd_model import cmd_model
from ember_code.backend.cmd_modes import cmd_accept, cmd_bypass, cmd_plan
from ember_code.backend.cmd_plugin import cmd_plugin, cmd_plugins
from ember_code.backend.cmd_schedule import cmd_schedule
from ember_code.backend.cmd_session import cmd_clear, cmd_fork, cmd_rename, cmd_sessions

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.backend.command_result import CommandResult


# All builtin shim callables share this signature: ``(handler, args)``
# → ``Awaitable[CommandResult]``. Two-arg shape for both no-arg
# commands (they accept an ignored ``args``) and arg-taking ones —
# the registry stays uniform.
BuiltinHandler = Callable[["CommandHandler", str], Awaitable["CommandResult"]]


@dataclass(frozen=True)
class BuiltinCommand:
    """One entry in the registry: slash-key + bare-name + description
    + shim callable.

    ``@dataclass(frozen=True)`` (not Pydantic) so ``handler`` — a
    Callable that Pydantic can't serialize without
    ``arbitrary_types_allowed=True`` — stores cleanly and the
    entry is hashable.
    """

    slash: str  # e.g. "/agents"
    description: str
    handler: BuiltinHandler

    @property
    def bare(self) -> str:
        """Name with the leading slash stripped."""
        return self.slash.lstrip("/")


class BuiltinCommandRegistry:
    """Registry of every built-in slash command.

    Adding a new builtin means one entry in :attr:`_ENTRIES` —
    the descriptions catalog and dispatch table are both derived
    from that single list. No more three-place edit.
    """

    def __init__(self) -> None:
        # Build the actual entries at instance-construct time so
        # the auth wrappers close over the underscored originals.
        self._entries: tuple[BuiltinCommand, ...] = tuple(self._build_entries())
        self._by_slash: Mapping[str, BuiltinCommand] = MappingProxyType(
            {e.slash: e for e in self._entries}
        )
        self._by_bare: Mapping[str, BuiltinCommand] = MappingProxyType(
            {e.bare: e for e in self._entries}
        )

    @staticmethod
    def _build_entries() -> list[BuiltinCommand]:
        async def _login_wrapper(handler: CommandHandler, _args: str) -> CommandResult:
            return await cmd_login(handler)

        async def _logout_wrapper(handler: CommandHandler, _args: str) -> CommandResult:
            return await cmd_logout(handler)

        async def _whoami_wrapper(handler: CommandHandler, _args: str) -> CommandResult:
            return await cmd_whoami(handler)

        async def _compact_wrapper(handler: CommandHandler, _args: str) -> CommandResult:
            return await cmd_compact(handler)

        async def _ctx_wrapper(handler: CommandHandler, _args: str) -> CommandResult:
            return await cmd_ctx(handler)

        async def _sync_knowledge_wrapper(handler: CommandHandler, _args: str) -> CommandResult:
            return await cmd_sync_knowledge(handler)

        async def _clear_wrapper(handler: CommandHandler, _args: str) -> CommandResult:
            return await cmd_clear(handler)

        async def _sessions_wrapper(handler: CommandHandler, _args: str) -> CommandResult:
            return await cmd_sessions(handler)

        return [
            BuiltinCommand("/quit", "Exit the session", cmd_quit),
            BuiltinCommand("/exit", "Exit the session", cmd_quit),
            BuiltinCommand("/help", "Show help and available commands", cmd_help),
            BuiltinCommand(
                "/watcher",
                "Open the background-process watcher panel",
                cmd_watcher,
            ),
            BuiltinCommand("/agents", "List and manage agents", cmd_agents),
            BuiltinCommand("/skills", "List installed skills", cmd_skills),
            BuiltinCommand("/hooks", "List installed hooks", cmd_hooks),
            BuiltinCommand("/clear", "Clear the current conversation", _clear_wrapper),
            BuiltinCommand("/sessions", "List past sessions", _sessions_wrapper),
            BuiltinCommand("/rename", "Rename the current session", cmd_rename),
            BuiltinCommand("/fork", "Fork the current session under a new id", cmd_fork),
            BuiltinCommand("/memory", "Inspect or optimise learned memories", cmd_memory),
            BuiltinCommand("/knowledge", "Search or add to the knowledge base", cmd_knowledge),
            BuiltinCommand("/codeindex", "Manage the semantic code index", cmd_codeindex),
            BuiltinCommand("/config", "Show current settings", cmd_config),
            BuiltinCommand("/model", "Switch the active model", cmd_model),
            BuiltinCommand("/mcp", "Open the MCP servers panel", cmd_mcp),
            BuiltinCommand("/login", "Sign in to Ember Cloud", _login_wrapper),
            BuiltinCommand("/logout", "Sign out of Ember Cloud", _logout_wrapper),
            BuiltinCommand("/whoami", "Show the signed-in user", _whoami_wrapper),
            BuiltinCommand("/schedule", "Schedule one-shot or recurring tasks", cmd_schedule),
            BuiltinCommand("/loop", "Run a prompt in a loop", cmd_loop),
            BuiltinCommand("/plugin", "Install / update / remove a plugin", cmd_plugin),
            BuiltinCommand("/plugins", "Open the plugins panel", cmd_plugins),
            BuiltinCommand("/compact", "Compact the conversation context", _compact_wrapper),
            BuiltinCommand("/ctx", "Show context window usage", _ctx_wrapper),
            BuiltinCommand(
                "/plan",
                "Toggle plan mode (read-only sandbox + plan-then-execute workflow)",
                cmd_plan,
            ),
            BuiltinCommand(
                "/accept",
                "Toggle acceptEdits mode (auto-approve file edits)",
                cmd_accept,
            ),
            BuiltinCommand(
                "/bypass",
                "Toggle bypassPermissions mode (auto-approve every tool — no HITL prompts)",
                cmd_bypass,
            ),
            BuiltinCommand(
                "/output-style",
                "List or switch the active output style (tone / verbosity)",
                cmd_output_style,
            ),
            BuiltinCommand("/bug", "Open a bug report", cmd_bug),
            BuiltinCommand("/evals", "Run evaluation suites", cmd_evals),
            BuiltinCommand(
                "/sync-knowledge",
                "Sync the knowledge base with git",
                _sync_knowledge_wrapper,
            ),
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

    def describe(self, name: str) -> str:
        """One-liner description for a built-in command. Accepts
        either ``"help"`` or ``"/help"``; returns ``""`` for
        unknown commands so callers can safely list every builtin
        without breaking if a description entry is missing.
        """
        entry = self.get(name)
        return entry.description if entry is not None else ""

    def as_mapping(self) -> Mapping[str, BuiltinHandler]:
        """Read-only view keyed on the slash form. Provided so the
        legacy :class:`CommandHandler._COMMANDS` back-compat shim
        can honour ``"/bypass" in _COMMANDS`` in existing tests
        (see :mod:`test_plan_mode`, :mod:`test_evals`).
        """
        return MappingProxyType({e.slash: e.handler for e in self._entries})

    async def dispatch(
        self, handler: CommandHandler, slash: str, args: str
    ) -> CommandResult | None:
        """Route ``slash`` to its shim. Returns ``None`` when the
        command isn't a registered builtin so the caller can fall
        through to the next dispatch tier (markdown, skills).
        """
        entry = self._by_slash.get(slash)
        if entry is None:
            return None
        return await entry.handler(handler, args)


# Module-level singleton — the registry is stateless (frozen
# entries) so one instance is safe to share across the process.
BUILTIN_REGISTRY = BuiltinCommandRegistry()


__all__ = ["BuiltinCommand", "BuiltinCommandRegistry", "BUILTIN_REGISTRY"]
