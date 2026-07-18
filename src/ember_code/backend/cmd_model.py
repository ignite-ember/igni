"""``/model`` slash command implementation.

Extracted from :mod:`ember_code.backend.command_handler` — the
old inline ``_cmd_model`` body validated the registry and
mutated ``settings.models.default`` + ``main_team`` directly,
including a private-attribute reach-in
(``self._session._build_main_agent()``). Now delegated to
:meth:`Session.set_default_model`, which returns a Pattern-3
:class:`ModelSwitchResult` envelope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session


class ModelCommand:
    """Coordinator for the ``/model`` slash-command family."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def dispatch(self, args: str) -> CommandResult:
        name = args.strip()
        if name:
            return self.switch(name)
        return self.open_picker()

    def switch(self, name: str) -> CommandResult:
        return self._session.set_default_model(name).to_command_result()

    def open_picker(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.MODEL)


async def cmd_model(handler: CommandHandler, args: str) -> CommandResult:
    """Two-line shim for :class:`ModelCommand`."""
    return await ModelCommand(handler.session).dispatch(args)
