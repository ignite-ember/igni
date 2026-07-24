"""``/help`` slash command implementation.

:class:`HelpCommand` composes a :class:`HelpTopicCatalog` and
routes ``/help`` args to either the interactive TUI panel (no
arg) or a named topic's markdown render. The topic corpus + the
"unknown topic" error live on :class:`HelpTopicCatalog` and
:class:`TopicNotFoundResult` in :mod:`schemas_help`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.backend.schemas_help import HelpTopicCatalog
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler


# Built once at import time; the catalog is stateless and shared
# across every ``/help`` invocation.
_DEFAULT_CATALOG = HelpTopicCatalog.default()


class HelpCommand:
    """Coordinator for the ``/help`` slash-command family.

    Holds a :class:`HelpTopicCatalog`; verb methods are
    :meth:`render_topic` and :meth:`open_panel`.
    """

    def __init__(self, catalog: HelpTopicCatalog) -> None:
        self._catalog = catalog

    def dispatch(self, args: str) -> CommandResult:
        topic = args.strip().lower()
        if not topic:
            return self.open_panel()
        return self._catalog.render(topic)

    def open_panel(self) -> CommandResult:
        """Open the interactive ``/help`` TUI panel."""
        return CommandResult.for_action(CommandAction.HELP)


async def cmd_help(handler: CommandHandler, args: str) -> CommandResult:
    """Two-line shim for :class:`HelpCommand`."""
    del handler  # HelpCommand has no session dependency.
    return HelpCommand(_DEFAULT_CATALOG).dispatch(args)
