"""``/codeindex`` slash command implementation.

Extracted from :mod:`ember_code.backend.command_handler` — the
2000-line god-file that holds every slash command. This is the
first of several planned extracts (one per command family) that
should collectively move ``command_handler.py`` out of the D
bucket.

Sub-commands handled here:

* ``/codeindex`` (no args) → open the TUI status panel via the
  ``CommandResult.for_action(CommandAction.CODEINDEX)`` action.
* ``/codeindex search <query>`` — semantic search over the
  current commit; renders markdown into chat.
* ``/codeindex item <id>`` — show full item details.
* ``/codeindex commits`` — list indexed commits.
* ``/codeindex clean`` — drop stale non-branch commits.
* ``/codeindex sync [sha]`` — pull + apply a changeset. Also
  handles the "needs install" branch that opens the GitHub App
  install page.
* ``/codeindex resync [sha]`` — wipe local state + pull fresh
  snapshot (drift recovery).
* ``/codeindex install`` — explicit "open install page" entry
  point.
* ``/codeindex status`` — show sync state + install progress.

Architecture: the eight verbs are methods on a single
:class:`CodeIndexCommand` coordinator, dispatched via a
``match`` inside :meth:`CodeIndexCommand.dispatch`. Presentation
lives in the sibling :mod:`schemas_codeindex` module — every
``.to_command_result()`` render call flows through a typed view.
The public ``cmd_codeindex(handler, args)`` entry point is a
two-line shim so :mod:`ember_code.backend.command_handler`'s
dispatch table stays intact.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ember_code.backend.browser_opener import BrowserOpener
from ember_code.backend.command_result import CommandResult
from ember_code.backend.schemas_codeindex import (
    CodeIndexCommitsView,
    CodeIndexHelpView,
    CodeIndexItemView,
    CodeIndexSearchView,
    CodeIndexStatusView,
    ResyncCommandView,
    SyncCommandView,
)
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class CodeIndexCommand:
    """Coordinator for the ``/codeindex`` slash-command family.

    Holds a :class:`Session` reference and exposes each verb as
    a bound method. Constructed per invocation so the coordinator
    stays stateless between calls (nothing outlives one
    ``dispatch()``).

    The class accepts a ``Session`` directly rather than the
    :class:`CommandHandler` state object, so we don't reach into
    ``handler._session`` from inside the coordinator (Rule 6:
    no private-attribute reach-in).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Dispatch ─────────────────────────────────────────────────

    async def dispatch(self, args: str) -> CommandResult:
        """Route a raw arg string to the matching verb method.

        The no-arg invocation opens the TUI panel via the
        ``CommandAction.CODEINDEX`` action; every other verb
        renders markdown/info/error into chat. Unknown verbs
        fall through to the help view.
        """
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        if not subcommand:
            return CommandResult.for_action(CommandAction.CODEINDEX)

        match subcommand:
            case "search":
                return await self.search(sub_args)
            case "item":
                return await self.item(sub_args)
            case "commits":
                return await self.commits()
            case "clean":
                return await self.clean()
            case "sync":
                return await self.sync(sub_args)
            case "resync":
                return await self.resync(sub_args)
            case "install":
                return await self.install()
            case "status":
                return await self.status()
            case _:
                return CodeIndexHelpView.to_command_result()

    # ── Verb methods ─────────────────────────────────────────────

    async def search(self, query: str) -> CommandResult:
        if not query:
            return CodeIndexHelpView.to_command_result()
        results = await self._session.code_index.search(query=query, limit=10)
        return CodeIndexSearchView(results=results).to_command_result()

    async def item(self, item_id: str) -> CommandResult:
        if not item_id:
            return CodeIndexHelpView.to_command_result()
        item_id = item_id.strip()
        item = await self._session.code_index.get_item(item_id=item_id)
        if item is None:
            return CommandResult.error(f"Item {item_id} not found.")
        return CodeIndexItemView(item=item).to_command_result()

    async def commits(self) -> CommandResult:
        state = self._session.code_index.manifest.load()
        return CodeIndexCommitsView(state=state).to_command_result()

    async def clean(self) -> CommandResult:
        dropped = await self._session.code_index.clean()
        if not dropped:
            return CommandResult.info("Nothing to clean.")
        return CommandResult.info(f"Dropped {len(dropped)} commit(s): {', '.join(dropped)}")

    async def sync(self, target_sha_arg: str) -> CommandResult:
        target_sha = target_sha_arg or None
        result = await self._session.code_index_sync.sync_now(sha=target_sha)
        self._refresh_availability_safely("sync")
        return SyncCommandView(result=result).to_command_result(open_browser=BrowserOpener.open)

    async def resync(self, target_sha_arg: str) -> CommandResult:
        # Wipe the local chroma for the target sha and pull a
        # fresh snapshot. Used when the local index drifts from
        # the cloud definition — e.g. an earlier sync took the
        # delta path with an absent parent and stored only the
        # diff's items.
        sync = self._session.code_index_sync
        target_sha = target_sha_arg or await asyncio.to_thread(sync.current_sha)
        if not target_sha:
            return CommandResult.error("Not a git repository — pass an explicit sha.")
        wiped = await self._session.code_index.forget_commit(target_sha)
        result = await sync.sync_now(sha=target_sha, force_snapshot=True)
        self._refresh_availability_safely("resync")
        view = ResyncCommandView(result=result, target_sha=target_sha)
        return view.to_command_result(open_browser=BrowserOpener.open, wiped=bool(wiped))

    async def install(self) -> CommandResult:
        # Explicit "open the install page for this repo" entry
        # point — useful when ``sync`` already succeeded but the
        # user wants to add a sibling repo, or revisit the
        # install screen.
        resolver = self._session.code_index_sync.resolver
        if resolver is None:
            return CommandResult.error("Resolver not available.")
        resolved = await resolver.resolve(force=True)
        if resolved is None:
            return CommandResult.error(
                "Could not reach Ember Cloud — check `/login` and `api_url`."
            )
        if not resolved.needs_install:
            return CommandResult.info(
                f"This repo is already registered (`{resolved.repository_id}`)."
            )
        if not resolved.install_url:
            return CommandResult.error(
                "Server didn't return an install URL — `github_app_slug` may be unset."
            )
        BrowserOpener.open(resolved.install_url)
        return CommandResult.markdown(
            f"### Install igniIndex\nOpening your browser:\n`{resolved.install_url}`"
        )

    async def status(self) -> CommandResult:
        index = self._session.code_index
        sync = self._session.code_index_sync
        # Both helpers shell out to ``git``; offload to a thread
        # so the BE's dispatcher keeps serving other sessions'
        # RPCs.
        local_sha = await asyncio.to_thread(sync.current_sha)
        remote_url = await asyncio.to_thread(sync.resolver.remote_url) if sync.resolver else None
        view = CodeIndexStatusView(
            local_sha=local_sha,
            remote_url=remote_url,
            last_synced=sync.last_synced_sha,
            index_head=index.head(),
            resolved=sync.resolver.cached if sync.resolver else None,
        )
        return view.to_command_result()

    # ── Private helpers ──────────────────────────────────────────

    def _refresh_availability_safely(self, verb: str) -> None:
        """Re-derive ``codeindex_available`` so the agent's prompt
        matches the post-sync chroma state.

        Kept as a bound helper so the two blanket ``except
        Exception`` blocks that used to live inline in ``_sync``
        and ``_resync`` collapse to one call site here. Reads the
        :class:`RefreshAvailabilityResult` returned by
        :meth:`Session.refresh_codeindex_availability` (which now
        catches exceptions and packages them into ``ok=False``)
        instead of wrapping the call in ``try / except``.
        """
        refresh = self._session.refresh_codeindex_availability()
        if not refresh.ok:
            logger.debug("refresh after /codeindex %s failed (%s)", verb, refresh.error)


async def cmd_codeindex(handler: CommandHandler, args: str) -> CommandResult:
    """Handle ``/codeindex`` commands.

    Two-line shim preserved verbatim so
    :mod:`ember_code.backend.command_handler` keeps importing
    ``cmd_codeindex`` by name and calling it with
    ``(self, args)``. All real work lives on
    :class:`CodeIndexCommand`.
    """
    return await CodeIndexCommand(handler.session).dispatch(args)
