"""Command handler — thin router for slash commands.

:class:`CommandHandler` composes three collaborators and dispatches
in three tiers:

* :class:`BuiltinCommandRegistry` — the ``/name → shim`` table plus
  ``/name → description`` catalog and dispatch method.
* :class:`MarkdownCommandDispatcher` — tier-2 fallback for
  markdown-authored ``.ember/commands/*.md`` files.
* :class:`SkillCommandDispatcher` — tier-3 fallback for
  user-invocable skills.

Skills lose to markdown-file names on collision because the user
explicitly imported the skill, whereas markdown files are best-effort
drop-ins.

Several symbols below are re-exported so ``mock.patch`` targets used
by the test suite (dotted paths under
``ember_code.backend.command_handler.*``) keep resolving.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from ember_code.backend.builtin_command_registry import (
    BUILTIN_REGISTRY,
    BuiltinCommand,  # noqa: F401 — public re-export
    BuiltinCommandRegistry,
)
from ember_code.backend.command_result import CommandResult
from ember_code.backend.markdown_command_dispatcher import (
    MarkdownCommandDispatcher,
)
from ember_code.backend.skill_command_dispatcher import SkillCommandDispatcher
from ember_code.core.auth.credentials import (  # noqa: F401 — mock.patch target
    CredentialsStore,
)
from ember_code.core.plugins.installer import (  # noqa: F401 — deprecated re-export
    PluginError,
    PluginInstaller,
)
from ember_code.core.plugins.marketplaces import (  # noqa: F401 — deprecated re-export
    add_marketplace,
    load_registry,
    refresh_marketplace,
    remove_marketplace,
    resolve_install_ref,
)

# The re-exports above existed as mock.patch targets for
# tests/test_plugins_slash_commands.py. That file has been migrated
# to patch at the source modules
# (``ember_code.core.plugins.installer.PluginInstaller``,
# ``ember_code.core.plugins.marketplaces.add_marketplace`` etc.) —
# matching test_plugins_backend.py. The re-exports here stay for
# ONE release cycle to unblock any external code still reaching for
# ``command_handler.PluginInstaller``. Delete after the deprecation
# window closes.
from ember_code.core.utils.markdown_commands import (  # noqa: F401 — mock.patch target
    discover_markdown_commands,
)
from ember_code.protocol.messages import (  # noqa: F401 — public re-export
    CommandAction,
    CommandResultKind,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ember_code.core.session import Session


class CommandHandler:
    """Thin slash-command router.

    Composes the built-in registry plus two fallback dispatchers and
    holds a reference to the owning :class:`Session`.
    """

    _REGISTRY: ClassVar[BuiltinCommandRegistry] = BUILTIN_REGISTRY

    def __init__(self, session: Session):
        self._session = session
        # Inject the module-level ``discover_markdown_commands``
        # symbol (re-exported above as a mock.patch target) into
        # the markdown dispatcher. Capturing the binding here means
        # tests that patch ``command_handler.discover_markdown_commands``
        # BEFORE constructing the handler land the patch on the
        # injected callable. Tests that build the handler first
        # must construct it INSIDE the patch block.
        self._markdown_dispatcher = MarkdownCommandDispatcher(
            session,
            discover_markdown_commands,
        )
        self._skill_dispatcher = SkillCommandDispatcher(session)

    @property
    def session(self) -> Session:
        """Public accessor for the owned :class:`Session` — used by
        every ``cmd_*.py`` coordinator to avoid private-attribute
        reach-in (Rule 6)."""
        return self._session

    async def handle(self, command: str) -> CommandResult:
        """Dispatch a slash command and return its result.

        Three tiers, in order: built-in registry, markdown fallback,
        skill fallback.
        """
        stripped = command.strip()
        cmd = stripped.split()[0].lower()
        args = stripped[len(cmd) :].strip()

        builtin = await self._REGISTRY.dispatch(self, cmd, args)
        if builtin is not None:
            return builtin

        md_result = await self._markdown_dispatcher.try_render(cmd, args)
        if md_result is not None:
            return md_result

        return await self._skill_dispatcher.match_and_render(stripped)

    async def _handle_markdown_command(self, cmd: str, args: str) -> CommandResult | None:
        """Delegate to :meth:`MarkdownCommandDispatcher.try_render`."""
        return await self._markdown_dispatcher.try_render(cmd, args)

    async def _handle_skill(self, stripped: str) -> CommandResult:
        """Delegate to :meth:`SkillCommandDispatcher.match_and_render`."""
        return await self._skill_dispatcher.match_and_render(stripped)

    @classmethod
    def builtin_names(cls) -> list[str]:
        """Bare (no-slash) names of every built-in command — the
        catalog surface consumed by :class:`SlashCommandsCatalog`."""
        return cls._REGISTRY.names()

    @classmethod
    def describe(cls, name: str) -> str:
        """One-liner description for a built-in command."""
        return cls._REGISTRY.describe(name)
