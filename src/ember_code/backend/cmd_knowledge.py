"""``/knowledge`` and ``/sync-knowledge`` slash command implementations.

Sibling of :mod:`cmd_memory` — the two commands share the
"knowledge base surface" concept but do not share the memory /
Learning Machine surface, so they live in their own module.

Sub-commands handled here:

* ``/knowledge`` (no args) — open the TUI panel via
  ``CommandResult.for_action(CommandAction.KNOWLEDGE)``, or emit
  an error card when the base failed to initialize.
* ``/knowledge add <url|path|text>`` — triage the argument into a
  URL / path / plain-text add.
* ``/knowledge search`` — open the panel; the panel's input
  field IS the search UI, so any positional argument is ignored
  by design (documented on :meth:`KnowledgeCommand.search`).
* ``/sync-knowledge`` (no args, separate slash command) —
  bidirectional cloud sync when ``knowledge.share=true``.

Architecture mirrors :mod:`cmd_codeindex` / :mod:`cmd_memory` —
:class:`KnowledgeCommand` is Session-injected, holds no
cross-invocation state, and every verb is a bound method. The
public shims (:func:`cmd_knowledge` and
:func:`cmd_sync_knowledge`) are two-liners that construct the
coordinator and delegate to the right method so
:mod:`ember_code.backend.command_handler`'s dispatch table stays
wire-compatible.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ember_code.backend.command_result import CommandResult
from ember_code.core.knowledge.models import KnowledgeSyncResult
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class KnowledgeSyncCommandView(BaseModel):
    """Typed view for the ``/sync-knowledge`` chat output.

    Wraps the ``list[KnowledgeSyncResult]`` returned by
    :meth:`SessionKnowledgeManager.sync_bidirectional` and renders
    each row as ``[<direction>] <summary>``, newline-joined.

    The join contract is byte-identical to the pre-refactor free
    function so downstream consumers (users' scrollback,
    integration tests that assert on the string) don't shift.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    results: list[KnowledgeSyncResult] = Field(default_factory=list)

    DISABLED_MESSAGE: ClassVar[str] = (
        "Knowledge sharing is not enabled. Set knowledge.share=true in config."
    )

    def to_command_result(self) -> CommandResult:
        lines = [f"[{r.direction}] {r.summary}" for r in self.results]
        return CommandResult.info("\n".join(lines))

    @classmethod
    def disabled(cls) -> CommandResult:
        return CommandResult.info(cls.DISABLED_MESSAGE)


class KnowledgeCommand:
    """Coordinator for the ``/knowledge`` and ``/sync-knowledge``
    slash-command families.

    Session-injected — no ``handler._session`` reach-in from
    inside the class. Constructed per invocation so it stays
    stateless between calls (matches the sibling
    :class:`~ember_code.backend.cmd_codeindex.CodeIndexCommand`
    contract).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Dispatch ─────────────────────────────────────────────────

    async def dispatch(self, args: str) -> CommandResult:
        """Route a raw arg string to the matching verb method.

        Recognised verbs:

        * ``add <url|path|text>`` → :meth:`add`
        * ``search`` (any positional arg ignored) → :meth:`search`
        * no arg / anything else → :meth:`panel`
        """
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        match subcommand:
            case "add" if sub_args:
                return await self.add(sub_args)
            case "search":
                return self.search()
            case _:
                return await self.panel()

    # ── Verb methods ─────────────────────────────────────────────

    async def add(self, sub_args: str) -> CommandResult:
        """Add a URL / path / text entry to the knowledge base.

        Triage is by shape:

        * starts with ``http://`` / ``https://`` → URL entry
        * contains a slash or leads with ``.`` → path entry
        * otherwise → plain-text entry
        """
        mgr = self._session.knowledge_mgr
        if sub_args.startswith(("http://", "https://")):
            result = await mgr.add_url(sub_args)
        elif "/" in sub_args or sub_args.startswith("."):
            result = await mgr.add_path(sub_args)
        else:
            result = await mgr.add(text=sub_args)
        if not result.success:
            return CommandResult.error(result.error)
        return CommandResult.info(result.message)

    def search(self) -> CommandResult:
        """Open the TUI knowledge panel.

        The panel's input field IS the search UI — pre-populating
        it from the slash-command line would defeat the point of
        the panel (interactive typing, iterating, browsing).
        Any positional argument after ``search`` is intentionally
        ignored; users continue typing inside the panel.
        """
        return CommandResult.for_action(CommandAction.KNOWLEDGE)

    async def panel(self) -> CommandResult:
        """Default no-arg entry: open the panel, or surface a
        clear error card when the base failed to initialize.

        Consumes :attr:`Session.knowledge_error` — the public
        property that surfaces the internal ``_knowledge_error``
        backing field — so this method never reaches into a
        private attribute.
        """
        mgr = self._session.knowledge_mgr
        status = await mgr.status()
        if not status.enabled:
            if self._session.settings.knowledge.enabled:
                err = self._session.knowledge_error
                if err:
                    return CommandResult.error(f"Knowledge failed to load: {err}")
                return CommandResult.error("Knowledge base failed to initialize.")
            return CommandResult.info(
                "Knowledge base is disabled. Set knowledge.enabled=true in config."
            )
        return CommandResult.for_action(CommandAction.KNOWLEDGE)

    async def sync(self) -> CommandResult:
        """Run a bidirectional cloud sync (``file_to_db`` +
        ``db_to_file``) when ``knowledge.share`` is enabled."""
        mgr = self._session.knowledge_mgr
        if not mgr.share_enabled():
            return KnowledgeSyncCommandView.disabled()
        results = await mgr.sync_bidirectional()
        return KnowledgeSyncCommandView(results=results).to_command_result()


async def cmd_knowledge(handler: CommandHandler, args: str) -> CommandResult:
    """Handle ``/knowledge`` commands.

    Two-line shim so :mod:`ember_code.backend.command_handler`
    keeps importing ``cmd_knowledge`` by name and calling it
    with ``(self, args)``. All real work lives on
    :class:`KnowledgeCommand`.
    """
    return await KnowledgeCommand(handler.session).dispatch(args)


async def cmd_sync_knowledge(handler: CommandHandler) -> CommandResult:
    """Handle ``/sync-knowledge`` — bidirectional cloud sync.

    Two-line shim so :mod:`ember_code.backend.command_handler`
    keeps importing ``cmd_sync_knowledge`` by name and calling it
    with ``(self,)`` — no args. All real work lives on
    :meth:`KnowledgeCommand.sync`.
    """
    return await KnowledgeCommand(handler.session).sync()
