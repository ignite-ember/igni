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

    def overview(self) -> str:
        """Bare ``/help`` as text, for a surface with no help panel.

        The panel action carries no content, so the plain prompt and
        ``igni -m /help`` printed nothing. This is what they print
        instead: every built-in command and every topic.
        """
        # Imported here: the registry imports this module's package.
        from ember_code.backend.builtin_command_registry import BUILTIN_REGISTRY

        lines = ["## Commands", ""]
        lines += [f"- `{e.slash}` — {e.description}" for e in BUILTIN_REGISTRY.entries()]
        lines += ["", "## Topics", ""]
        lines.append(", ".join(f"`/help {name}`" for name in self._catalog.names()))
        return "\n".join(lines)


def help_overview() -> str:
    """The text a bare ``/help`` prints where there is no panel."""
    return HelpCommand(_DEFAULT_CATALOG).overview()


async def cmd_help(handler: CommandHandler, args: str) -> CommandResult:
    """Two-line shim for :class:`HelpCommand`."""
    del handler  # HelpCommand has no session dependency.
    return HelpCommand(_DEFAULT_CATALOG).dispatch(args)
