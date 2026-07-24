"""Shared :class:`SlashCommand` Protocol.

Every OOP-refactored ``cmd_*`` package (starting with
:mod:`ember_code.backend.cmd_plugin`) exposes one or more
:class:`SlashCommand` instances. The :class:`BuiltinCommandRegistry`
accepts both this shape and the older bare-async-callable shape
during the transition — see :func:`_normalize_entry` in the
registry.

Kept in its own module (not inside the registry file) so any
``cmd_*.py`` package can implement the protocol without importing
the registry back — that would cycle at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.backend.command_result import CommandResult


@runtime_checkable
class SlashCommand(Protocol):
    """Minimum surface a slash-command implementation must expose.

    ``name`` is the leading-slash form (``"/plugin"``). ``description``
    feeds :class:`SlashCommandsCatalog`. ``run(handler, args)`` is the
    dispatch entrypoint the registry calls.
    """

    name: str
    description: str

    async def run(self, handler: CommandHandler, args: str) -> CommandResult:
        """Execute the command. ``args`` is the arg suffix after the
        leading slash-name token (already stripped of the command
        name itself)."""
        ...


__all__ = ["SlashCommand"]
