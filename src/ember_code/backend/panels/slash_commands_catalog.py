"""Slash-commands catalog controller.

Owns the merged builtin + markdown + user-invocable-skill slash
command enumeration for SDK consumers (IDE plugins, completion
UIs). Three private methods (:meth:`_builtin_entries`,
:meth:`_markdown_entries`, :meth:`_skill_entries`) each build one
source's typed sub-list; :meth:`entries` orchestrates them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.backend.command_handler import CommandHandler
from ember_code.backend.schemas_panels import (
    BuiltinSlashCommand,
    MarkdownSlashCommand,
    SkillSlashCommand,
    SlashCommandEntry,
)
from ember_code.core.utils.markdown_commands import discover_markdown_commands

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class SlashCommandsCatalog:
    """Merged builtin + markdown + user-invocable-skill catalog."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def entries(self) -> list[SlashCommandEntry]:
        """All slash commands available in this session."""
        out: list[SlashCommandEntry] = []
        out.extend(self._builtin_entries())
        out.extend(self._markdown_entries())
        out.extend(self._skill_entries())
        return out

    def _builtin_entries(self) -> list[BuiltinSlashCommand]:
        """Built-in commands from :class:`CommandHandler`."""
        return [
            BuiltinSlashCommand.from_builtin(
                name=bare,
                description=CommandHandler.describe(bare),
            )
            for bare in CommandHandler.builtin_names()
        ]

    def _markdown_entries(self) -> list[MarkdownSlashCommand]:
        """Markdown-authored commands under ``.ember/commands/`` /
        ``.claude/commands/`` (CC parity)."""
        try:
            read_claude = self._session.settings.rules.cross_tool_support
            md_commands = discover_markdown_commands(
                self._session.project_dir,
                read_claude=read_claude,
            )
        except Exception as exc:
            logger.debug("slash_commands: markdown discovery failed: %s", exc)
            return []
        return [MarkdownSlashCommand.from_markdown(md) for md in md_commands.values()]

    def _skill_entries(self) -> list[SkillSlashCommand]:
        """User-invocable skills."""
        try:
            skills = self._session.skill_pool.list_skills()
        except Exception as exc:
            logger.debug("slash_commands: skill enumeration failed: %s", exc)
            return []
        return [
            SkillSlashCommand.from_skill(skill)
            for skill in skills
            if getattr(skill, "user_invocable", True)
        ]
