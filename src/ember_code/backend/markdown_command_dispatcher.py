"""Markdown-authored slash-command dispatcher.

Extracted from :mod:`ember_code.backend.command_handler` — the
old ``_handle_markdown_command`` was an async method on the
handler that did the discover + render dance inline, with two
blanket ``except Exception`` blocks. Now a
:class:`MarkdownCommandDispatcher` class that owns the same
behaviour but keeps the wide except in one place with a
documented rationale.

Discovery happens per-invocation rather than at session init —
the cost is a handful of stat() calls + a small YAML parse,
dwarfed by the LLM round-trip that follows, and avoids stale
caching when a user is iterating on a command file in another
editor.

Discovery is a constructor-injected dependency
(:attr:`_discover`, typed via :data:`DiscoverMarkdownCommands`)
supplied by the composing :class:`CommandHandler` — not a
sibling-module reach-in at call time. Tests patch
``ember_code.backend.command_handler.discover_markdown_commands``
BEFORE constructing the handler, so the patched symbol is what
gets captured as the injected callable. The same path through
``ember_code.backend.markdown_command_dispatcher
.discover_markdown_commands`` is no longer valid here (no module-
level alias), so the test helper stacks both patch targets.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ember_code.backend.command_result import CommandResult
from ember_code.core.utils.markdown_commands import MarkdownCommand, discover_markdown_commands
from ember_code.protocol.messages import CommandAction, CommandResultKind

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)

__all__ = ["MarkdownCommandDispatcher", "discover_markdown_commands"]


class DiscoverMarkdownCommands(Protocol):
    """Callable signature for the discovery dependency injected into
    :class:`MarkdownCommandDispatcher`.

    Matches :func:`ember_code.core.utils.markdown_commands
    .discover_markdown_commands` — kept as a Protocol (not a
    direct import reference) so tests can substitute a bare
    ``MagicMock`` without pulling in the real discovery chain.
    """

    def __call__(
        self,
        project_dir: Path,
        *,
        read_claude: bool = True,
    ) -> dict[str, MarkdownCommand]: ...


class MarkdownCommandDispatcher:
    """Match a slash-command name against markdown-authored commands
    under ``.ember/commands/`` (and ``.claude/commands/`` when
    cross-tool support is on) and render the body to a prompt.

    :meth:`try_render` returns ``None`` to signal "not a markdown
    command — fall through to the next dispatcher tier". A render
    failure surfaces as an error :class:`CommandResult` because
    the user explicitly invoked THIS command (falling through
    silently would make them think it doesn't exist).
    """

    def __init__(
        self,
        session: Session,
        discover: DiscoverMarkdownCommands,
    ) -> None:
        """Compose a session and a discovery callable.

        ``discover`` is captured at construction — tests must
        construct (or reconstruct) the handler INSIDE the
        ``with patch(...)`` block so the patched symbol is what
        gets injected. The composing :class:`CommandHandler`
        threads the module-level
        ``ember_code.backend.command_handler
        .discover_markdown_commands`` reference, so any patch on
        that path before handler construction lands here.
        """
        self._session = session
        self._discover = discover

    async def try_render(self, cmd: str, args: str) -> CommandResult | None:
        name = cmd.lstrip("/")
        if not name:
            return None
        # Wide except because the discovery layer walks the file
        # system + parses YAML frontmatter — a broken file
        # (permissions, invalid YAML, deleted mid-walk) should
        # fall through to the next tier rather than surface as a
        # user error. Kept in one place; not scattered.
        try:
            read_claude = self._session.settings.rules.cross_tool_support
            commands = self._discover(
                self._session.project_dir,
                read_claude=read_claude,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Markdown command discovery failed: %s", exc)
            return None
        md = commands.get(name)
        if md is None:
            return None
        # Render failure is DIFFERENT from discovery failure — the
        # user picked THIS command; surface the error.
        try:
            rendered = await md.render(args, project_dir=self._session.project_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Markdown command /%s render failed: %s", name, exc)
            return CommandResult.error(f"/{name}: render failed: {exc}")
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=rendered,
            action=CommandAction.RUN_PROMPT,
        )
