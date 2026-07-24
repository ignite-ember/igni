"""``/memory`` slash command implementation.

Extracted (and slimmed) from the older ``cmd_memory.py`` that
housed ``/memory`` + ``/knowledge`` + ``/sync-knowledge`` as three
free functions. Following the sibling :mod:`cmd_codeindex` pattern:

* Only ``/memory`` lives here now. The ``/knowledge`` and
  ``/sync-knowledge`` commands moved to
  :mod:`ember_code.backend.cmd_knowledge`.
* All work happens on :class:`MemoryCommand`, a Session-injected
  coordinator with one method per verb. The public
  :func:`cmd_memory` entry point is a two-line shim so
  :mod:`ember_code.backend.command_handler`'s dispatch table stays
  wire-compatible.
* Payload shaping (untyped ``arecall`` dict → per-store Pydantic
  section models) lives in :mod:`schemas_memory`. This module is
  the coordinator only.

Sub-commands handled here:

* ``/memory`` (no args) — show Learning Machine data (user
  profile / memory / entity memory / session context).
* ``/memory optimize`` — trigger a compaction pass over stored
  memories via :class:`SessionMemoryManager`.

Sibling command files (:mod:`cmd_session`, :mod:`cmd_modes`,
:mod:`cmd_plugin`) remain procedural at the time of this
refactor — that shape is documented tech debt and will be
migrated in follow-up passes. :mod:`cmd_schedule` has since
adopted the coordinator + view-model pattern (see
:class:`~ember_code.backend.cmd_schedule.ScheduleCommand`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.backend.schemas_memory import LearningRecall

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class MemoryCommand:
    """Coordinator for the ``/memory`` slash-command family.

    Holds a :class:`Session` reference and exposes each verb as
    a bound method. Constructed per invocation so the coordinator
    stays stateless between calls (nothing outlives one
    :meth:`dispatch` call).

    The class accepts a :class:`Session` directly rather than the
    :class:`CommandHandler` state object, so we don't reach into
    ``handler._session`` from inside the coordinator (Rule 6: no
    private-attribute reach-in). Matches the sibling
    :class:`~ember_code.backend.cmd_codeindex.CodeIndexCommand`
    contract.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Dispatch ─────────────────────────────────────────────────

    async def dispatch(self, args: str) -> CommandResult:
        """Route a raw arg string to the matching verb method.

        Recognised verbs:

        * ``optimize`` → :meth:`optimize`
        * anything else (including empty args) → :meth:`show`
        """
        subcommand = args.strip().lower()
        if subcommand == "optimize":
            return await self.optimize()
        return await self.show()

    # ── Verb methods ─────────────────────────────────────────────

    async def optimize(self) -> CommandResult:
        """Trigger a compaction pass over stored memories via
        :class:`SessionMemoryManager`."""
        result = await self._session.memory_mgr.optimize()
        if not result.success:
            return CommandResult.error(f"Memory optimization failed: {result.error}")
        return CommandResult.info(result.message)

    async def show(self) -> CommandResult:
        """Render the Learning Machine's cross-session recall
        (user profile / memory / entity memory / session context).

        Uses :attr:`Session.learning_machine` — the public
        property that fuses ``main_team.learning_machine`` with
        the fallback ``_learning`` field, so this method never
        touches Session privates. Recall failures collapse to
        the empty-recall path inside
        :meth:`LearningRecall.aload`."""
        learning = self._session.learning_machine
        if learning is None:
            return CommandResult.info(
                "Learning is not enabled. Set learning.enabled=true in config."
            )
        recall = await LearningRecall.aload(learning, self._session.user_id)
        return recall.to_command_result()


async def cmd_memory(handler: CommandHandler, args: str) -> CommandResult:
    """Handle ``/memory`` commands.

    Two-line shim preserved verbatim so
    :mod:`ember_code.backend.command_handler` keeps importing
    ``cmd_memory`` by name and calling it with ``(self, args)``.
    All real work lives on :class:`MemoryCommand`.
    """
    return await MemoryCommand(handler.session).dispatch(args)
