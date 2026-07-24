"""``/agents`` slash command implementation.

Extracted from :mod:`ember_code.backend.command_handler` — the
old inline ``_cmd_agents`` body handled four sub-commands
(``promote``, ``discard``, ``ephemeral``, and default → open
panel) in a linear if/elif ladder. Now split into verb methods
on :class:`AgentsCommand` with a ``dispatch(args)`` router.

The pool's ``promote_ephemeral`` / ``discard_ephemeral`` still
raise (tests pin that behaviour); the coordinator catches once
and packages the outcome into a :class:`PromoteResult` /
:class:`DiscardResult` so the try/except lives in a single spot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.backend.schemas_agents import (
    DiscardResult,
    EphemeralAgentsView,
    PromoteResult,
)
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session


class AgentsCommand:
    """Coordinator for the ``/agents`` slash-command family.

    Holds a :class:`Session` reference (not the handler) so we
    never reach into ``handler._session`` from inside the
    coordinator. Verb methods are ``promote / discard /
    list_ephemeral / open_panel``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    async def dispatch(self, args: str) -> CommandResult:
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        match subcommand:
            case "promote":
                if not sub_args:
                    return CommandResult.error("Usage: /agents promote <name>")
                return self.promote(sub_args.strip())
            case "discard":
                if not sub_args:
                    return CommandResult.error("Usage: /agents discard <name>")
                return self.discard(sub_args.strip())
            case "ephemeral":
                return self.list_ephemeral()
            case _:
                return self.open_panel()

    # ── Verb methods ─────────────────────────────────────────────

    def promote(self, name: str) -> CommandResult:
        try:
            dest = self._session.pool.promote_ephemeral(name, self._session.project_dir)
            result = PromoteResult(ok=True, destination=str(dest))
        except (KeyError, ValueError, RuntimeError) as exc:
            result = PromoteResult(ok=False, error=str(exc))
        return result.to_command_result(name)

    def discard(self, name: str) -> CommandResult:
        try:
            self._session.pool.discard_ephemeral(name)
            result = DiscardResult(ok=True)
        except (KeyError, ValueError, RuntimeError) as exc:
            result = DiscardResult(ok=False, error=str(exc))
        return result.to_command_result(name)

    def list_ephemeral(self) -> CommandResult:
        agents = self._session.pool.list_ephemeral()
        return EphemeralAgentsView(agents=agents).to_command_result()

    def open_panel(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.AGENTS)


async def cmd_agents(handler: CommandHandler, args: str) -> CommandResult:
    """Two-line shim for :class:`AgentsCommand`."""
    return await AgentsCommand(handler.session).dispatch(args)
