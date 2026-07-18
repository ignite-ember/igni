"""Session-management slash commands.

Extracted from :mod:`ember_code.backend.command_handler` — four
commands that shape the session-persistence surface:

* ``/clear`` — rotate to a fresh ``session_id``, cutting the
  agent's history without deleting the source session.
* ``/sessions`` — open the sessions panel (returns the panel
  action; TUI handles the rest).
* ``/rename <name>`` — set the display name on the current
  session row.
* ``/fork [name]`` — clone the current session under a new id
  and switch to it. Optional argument is the new session's
  display name.

The critical invariant across the mutating commands (``/clear``
and ``/fork``): whenever ``session_id`` rotates, it MUST be
propagated to ``main_team.session_id`` AND
``persistence.session_id`` — Agno keys persistence on
``team.session_id``, not on ``_session.session_id``. Missing
either causes the agent to read the old session's history
while the FE displays the new one. That three-attribute
invariant is encapsulated on :meth:`Session.rotate_id`, so
this module never touches the three attrs directly.

Architecture mirrors :mod:`ember_code.backend.cmd_codeindex`
(the canonical OOP reference in the ``cmd_*`` family): the
four verbs are methods on a single :class:`SessionCommand`
coordinator; module-level ``cmd_*`` functions are two-line
shims so :mod:`ember_code.backend.command_handler`'s dispatch
table keeps importing them by name.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class SessionCommand:
    """Coordinator for the session-management slash-command family.

    Holds a :class:`Session` reference and exposes each verb as
    a bound method. Constructed per invocation so the coordinator
    stays stateless between calls (nothing outlives one dispatch).

    The class accepts a ``Session`` directly rather than the
    :class:`CommandHandler` state object, so we don't reach into
    ``handler._session`` from inside the coordinator (Rule 6:
    no private-attribute reach-in). See
    :class:`ember_code.backend.cmd_codeindex.CodeIndexCommand`
    for the canonical OOP reference this class mirrors.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Verb methods ─────────────────────────────────────────────

    async def clear(self) -> CommandResult:
        """Rotate to a fresh ``session_id`` — starts new agent history.

        Fires a background codeindex resync + prompt refresh so the
        fresh agent's system prompt matches the post-sync chroma
        state.
        """
        # Generate new session_id so Agno starts fresh history — see
        # :meth:`Session.rotate_id` for why every component must be
        # re-bound in one step.
        new_id = str(uuid.uuid4())[:8]
        self._session.rotate_id(new_id)
        # Latched ctx counter belongs to the previous conversation;
        # the backend reads this and exposes get_status().
        self._session.latch_input_tokens(0)

        # New dialogue → re-pull the changeset for current HEAD
        # (fire-and-forget). Also refresh the codeindex_available
        # flag afterwards so the rebuilt agent's system prompt
        # matches the post-sync chroma state.
        asyncio.create_task(self._sync_then_refresh())
        return CommandResult.for_action(CommandAction.CLEAR)

    async def sessions(self) -> CommandResult:
        """Open the sessions panel — TUI handles the rest."""
        return CommandResult.for_action(CommandAction.SESSIONS)

    async def rename(self, args: str) -> CommandResult:
        """Set the display name on the current session row."""
        name = args.strip()
        if not name:
            return CommandResult.error("Usage: /rename <new session name>")
        await self._session.persistence.rename(name)
        return CommandResult.info(f"Session renamed to: {name}")

    async def fork(self, args: str) -> CommandResult:
        """Clone the current session under a fresh id and switch to it.

        Optional argument is the new session's display name; without
        it the fork inherits the source's name (or stays nameless,
        same as a freshly-created session that auto-names after the
        first run completes).
        """
        name = args.strip() or None
        try:
            new_id = await self._session.persistence.fork(name=name)
        except Exception as exc:
            logger.warning("/fork failed: %s", exc)
            return CommandResult.error(f"Fork failed: {exc}")
        # Re-bind every component that holds the active session id
        # so the next user turn lands in the fork, not the source.
        self._session.rotate_id(new_id)
        self._session.session_named = bool(name)
        return CommandResult.fork(new_id)

    # ── Private helpers ──────────────────────────────────────────

    async def _sync_then_refresh(self) -> None:
        """Post-``/clear`` fire-and-forget side effect.

        Kept as a bound method (rather than the previous nested
        ``async def`` inside :meth:`clear`) so the closure over
        ``session`` collapses to ``self._session`` — mirrors
        :meth:`CodeIndexCommand._refresh_availability_safely`.
        """
        await self._session.code_index_sync.sync_now()
        refresh = self._session.refresh_codeindex_availability()
        if not refresh.ok:
            logger.debug("refresh after /clear sync failed (%s)", refresh.error)


# ── Module-level shims ───────────────────────────────────────────
#
# Preserved verbatim so :mod:`ember_code.backend.command_handler`
# keeps importing ``cmd_clear`` / ``cmd_sessions`` / ``cmd_rename``
# / ``cmd_fork`` by name and calling them with ``(self)`` or
# ``(self, args)``. All real work lives on :class:`SessionCommand`.


async def cmd_clear(handler: CommandHandler) -> CommandResult:
    """See :meth:`SessionCommand.clear`."""
    return await SessionCommand(handler.session).clear()


async def cmd_sessions(handler: CommandHandler) -> CommandResult:
    """See :meth:`SessionCommand.sessions`."""
    return await SessionCommand(handler.session).sessions()


async def cmd_rename(handler: CommandHandler, args: str) -> CommandResult:
    """See :meth:`SessionCommand.rename`."""
    return await SessionCommand(handler.session).rename(args)


async def cmd_fork(handler: CommandHandler, args: str) -> CommandResult:
    """See :meth:`SessionCommand.fork`."""
    return await SessionCommand(handler.session).fork(args)
