"""Skills panel controller.

Owns the skills panel + input-autocomplete concern. Snapshots
the skill pool for the panel and exposes the pool + names for
autocomplete UIs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.core.skills import SkillPool
from ember_code.core.skills.parser import SkillInfo

if TYPE_CHECKING:
    from ember_code.core.session import Session


class SkillsPanelController:
    """Snapshot + autocomplete accessors for the skills panel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def snapshot(self) -> list[SkillInfo]:
        """Snapshot of every loaded skill for the panel UI."""
        return [
            SkillInfo(
                name=skill.name,
                description=skill.description,
                version=skill.version,
                category=skill.category,
                argument_hint=skill.argument_hint,
                context=skill.context,
                agent=skill.agent or "",
                user_invocable=skill.user_invocable,
                body=skill.body,
                source_dir=str(skill.source_dir) if skill.source_dir else "",
            )
            for skill in self._session.skill_pool.list_skills()
        ]

    def pool(self) -> SkillPool:
        """Return the skill pool for input autocomplete."""
        return self._session.skill_pool

    def names(self) -> list[str]:
        """Skill names for input autocomplete."""
        return [s.name for s in self._session.skill_pool.list_skills()]
