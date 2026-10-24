"""``/artifacts`` — open the documents page.

This was two hundred and thirty-seven lines: ``/artifacts add``
wrote through the session's artifact manager, ``/artifacts search``
queried the local index, and the bare form asked the store whether
it was healthy before deciding what to say.

None of that is igni's any more. The store moved to
``igni__artifacts/extension`` and the manager went with it, so a
command that reached for ``session.artifact_mgr`` is reaching for
something the host does not build. What is left is the half that
was always the host's: opening the page.

**Why it is still a builtin at all.** The extension declares
``/artifacts`` in ``contributes.commands``, and the dispatcher
would find it — but a contributed command dispatches to a *tool*
of the same name, and "open a page" is not something a tool can
do. The page is igni's chrome. When the panels move into the
extension's own frame this command goes with them, along with the
fifty-nine RPC names in
:mod:`ember_code.backend.rpc_handlers.artifacts`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.protocol.messages import CommandAction
from ember_code.backend.command_result import CommandResult

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler


class ArtifactsCommand:
    """``/artifacts`` — go to the documents page."""

    def open_panel(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.ARTIFACTS)


async def cmd_artifacts(_handler: CommandHandler, _args: str) -> CommandResult:
    """Two-line shim for :class:`ArtifactsCommand`."""
    return ArtifactsCommand().open_panel()
