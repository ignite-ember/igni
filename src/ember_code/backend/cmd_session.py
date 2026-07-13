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
while the FE displays the new one.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler, CommandResult

logger = logging.getLogger(__name__)


async def cmd_clear(handler: "CommandHandler") -> "CommandResult":
    """Rotate to a fresh ``session_id`` — starts new agent history.

    Fires a background codeindex resync + prompt refresh so the
    fresh agent's system prompt matches the post-sync chroma
    state.
    """
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    session = handler._session

    # Generate new session_id so Agno starts fresh history. The id
    # MUST be propagated to main_team and persistence — otherwise
    # team.arun keeps reading the old session's history (Agno keys
    # persistence on team.session_id, not on _session.session_id)
    # and count_context_tokens queries an empty new id, making the
    # footer read 0% while the agent silently continues the old
    # conversation.
    new_id = str(uuid.uuid4())[:8]
    session.session_id = new_id
    session.main_team.session_id = new_id
    session.persistence.session_id = new_id
    # Latched ctx counter belongs to the previous conversation; the
    # backend reads this and exposes get_status().
    session._last_input_tokens = 0

    # New dialogue → re-pull the changeset for current HEAD
    # (fire-and-forget). Also refresh the codeindex_available flag
    # afterwards so the rebuilt agent's system prompt matches the
    # post-sync chroma state.
    async def _sync_then_refresh() -> None:
        await session.code_index_sync.sync_now()
        try:
            session.refresh_codeindex_availability()
        except Exception as exc:
            logger.debug("refresh after /clear sync failed (%s)", exc)

    asyncio.create_task(_sync_then_refresh())
    return CommandResult.clear()


async def cmd_sessions(handler: "CommandHandler") -> "CommandResult":
    """Open the sessions panel — TUI handles the rest."""
    from ember_code.backend import command_handler as _handler

    return _handler.CommandResult.sessions()


async def cmd_rename(handler: "CommandHandler", args: str) -> "CommandResult":
    """Set the display name on the current session row."""
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    name = args.strip()
    if not name:
        return CommandResult.error("Usage: /rename <new session name>")
    await handler._session.persistence.rename(name)
    return CommandResult.info(f"Session renamed to: {name}")


async def cmd_fork(handler: "CommandHandler", args: str) -> "CommandResult":
    """Clone the current session under a fresh id and switch to it.

    Optional argument is the new session's display name; without
    it the fork inherits the source's name (or stays nameless,
    same as a freshly-created session that auto-names after the
    first run completes).
    """
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    session = handler._session
    name = args.strip() or None
    try:
        new_id = await session.persistence.fork(name=name)
    except Exception as exc:
        logger.warning("/fork failed: %s", exc)
        return CommandResult.error(f"Fork failed: {exc}")
    # Re-bind every component that holds the active session id so the
    # next user turn lands in the fork, not the source.
    session.session_id = new_id
    session.session_named = bool(name)
    session.main_team.session_id = new_id
    session.persistence.session_id = new_id
    return CommandResult.fork(new_id)
