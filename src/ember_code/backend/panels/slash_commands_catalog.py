"""Slash-commands catalog controller.

Owns the merged builtin + markdown + skill + extension slash command
enumeration for SDK consumers (IDE plugins, completion UIs). Four
private methods each build one source's typed sub-list;
:meth:`entries` orchestrates them.

The extension source was the last to arrive, and until it did
``contributes.commands`` was a manifest field nothing read: an
extension could declare ``/compliance``, it never appeared in
completion, and typing it got the skill fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.backend.command_handler import CommandHandler
from ember_code.backend.schemas_panels import (
    BuiltinSlashCommand,
    ExtensionSlashCommand,
    MarkdownSlashCommand,
    SkillSlashCommand,
    SlashCommandEntry,
)
from ember_code.core.contributed_commands import (
    ContributedCommands,
    contributed_commands,
)
from ember_code.core.utils.markdown_commands import discover_markdown_commands

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class SlashCommandsCatalog:
    """Merged builtin + markdown + user-invocable-skill catalog."""

    def __init__(self, session: Session, commands: ContributedCommands | None = None) -> None:
        self._session = session
        self._commands = commands if commands is not None else contributed_commands

    def entries(self) -> list[SlashCommandEntry]:
        """All slash commands available in this session."""
        out: list[SlashCommandEntry] = []
        out.extend(self._builtin_entries())
        out.extend(self._markdown_entries())
        out.extend(self._skill_entries())
        out.extend(self._extension_entries())
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
        """Markdown-authored commands under ``.igni/commands/`` /
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

    def _extension_entries(self) -> list[ExtensionSlashCommand]:
        """Commands contributed by installed extensions.

        Only what is live. The registry holds a command while its
        extension is READY and drops it otherwise, so a command in
        this list is one that can actually answer — offering one that
        cannot is worse than not offering it, because the user picks
        it and then it fails.
        """
        return [ExtensionSlashCommand.from_contributed(command) for command in self._commands.all()]
