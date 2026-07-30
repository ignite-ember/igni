"""``/hooks`` slash command implementation.

Extracted from :mod:`ember_code.backend.command_handler` — the
old inline ``_cmd_hooks`` body handled the three verbs
(``reload``, ``list``, default → open panel) inline. Now split
into methods on :class:`HooksCommand` with a ``dispatch(args)``
router. Presentation moved to :mod:`schemas_hooks`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.backend.schemas_hooks import HooksListView, HooksReloadResult
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session


class HooksCommand:
    """Coordinator for the ``/hooks`` slash-command family."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def dispatch(self, args: str) -> CommandResult:
        subcommand = args.strip().lower()
        match subcommand:
            case "reload":
                return self.reload()
            case "list":
                return self.list_hooks()
            case _:
                return self.open_panel()

    # ── Verb methods ─────────────────────────────────────────────

    def reload(self) -> CommandResult:
        count = self._session.reload_hooks()
        return HooksReloadResult(count=count).to_command_result()

    def list_hooks(self) -> CommandResult:
        return HooksListView(hooks_map=self._session.hooks_map).to_command_result()

    def open_panel(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.HOOKS)


async def cmd_hooks(handler: CommandHandler, args: str) -> CommandResult:
    """Two-line shim for :class:`HooksCommand`."""
    return await HooksCommand(handler.session).dispatch(args)
