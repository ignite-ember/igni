"""Skill-command dispatcher (fallback tier of the slash router).

Extracted from :mod:`ember_code.backend.command_handler` — the
old ``_handle_skill`` was an async method on the handler that
matched a command against the skill pool and either fed the
rendered prompt into the main run loop or returned an
``Unknown command`` error. Now a :class:`SkillCommandDispatcher`
class so the last-tier fallback is a single object rather than a
loose method.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.protocol.messages import CommandAction, CommandResultKind

if TYPE_CHECKING:
    from ember_code.core.session import Session


class SkillCommandDispatcher:
    """Match a stripped slash command against the session's skill
    pool. On a hit, render the skill body into a
    :class:`CommandResult` with the ``RUN_PROMPT`` action so the
    main streaming run loop feeds it into the agent. On a miss,
    return the terminal ``Unknown command`` error — this is the
    last-tier fallback of the slash router.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    async def match_and_render(self, stripped: str) -> CommandResult:
        skill_match = self._session.skill_pool.match_user_command(stripped)
        if skill_match:
            skill, args = skill_match
            rendered = skill.render(args, session_id=self._session.session_id)
            return CommandResult(
                kind=CommandResultKind.INFO,
                content=rendered,
                action=CommandAction.RUN_PROMPT,
            )
        return CommandResult.error(f"Unknown command: {stripped.split()[0]}")
