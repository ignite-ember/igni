"""Shared :class:`SlashCommand` Protocol.

Every OOP-refactored ``cmd_*`` package (starting with
:mod:`ember_code.backend.cmd_plugin`) exposes one or more
:class:`SlashCommand` instances, and types its exported tuple
against this Protocol so a command that drifts from the shape fails
type-check at its own module rather than at some future dispatch.

:class:`BuiltinCommandRegistry` does **not** consume this shape yet —
it still stores the bare-async-callable ``(handler, args) ->
CommandResult`` form only (``BuiltinHandler``), which each ``cmd_*``
package provides as a thin shim over its command objects. This
docstring used to point at a ``_normalize_entry`` in the registry
that accepted both shapes; no such function was ever written.

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
