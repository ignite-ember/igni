"""``/codeindex`` slash command implementation.

Extracted from :mod:`ember_code.backend.command_handler` — the
2000-line god-file that holds every slash command. This is the
first of several planned extracts (one per command family) that
should collectively move ``command_handler.py`` out of the D
bucket.

Sub-commands handled here:

* ``/codeindex`` (no args) → open the TUI status panel via the
  ``CommandResult.codeindex()`` action.
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
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.backend.command_handler import CommandResult

logger = logging.getLogger(__name__)


def _open_in_browser(url: str) -> None:
    """Best-effort open in browser; failures are logged, never raised."""
    try:
        webbrowser.open(url)
    except Exception as exc:  # pragma: no cover — platform-dependent
        logger.info("could not open browser for %s: %s", url, exc)


async def cmd_codeindex(handler: "CommandHandler", args: str) -> "CommandResult":
    """Handle ``/codeindex`` commands.

    No-arg invocation opens the TUI status panel (current-commit
    indexed state + sync/clean/install verb keys, with a 2s live
    status poll). Search lives on ``/codeindex search <query>``
    and renders markdown into chat — results are better-suited
    to chat history than to an ephemeral bottom panel.

    The remaining subcommands (``item``, ``commits``, ``clean``,
    ``sync``, ``install``, ``status``) keep their chat output as
    a power-user / scripting fallback.
    """
    from ember_code.backend.command_handler import CommandResult

    session = handler._session
    index = session.code_index
    sync = session.code_index_sync
    parts = args.strip().split(None, 1)
    subcommand = parts[0].lower() if parts else ""
    sub_args = parts[1].strip() if len(parts) > 1 else ""

    if not subcommand:
        return CommandResult.codeindex()

    if subcommand == "search" and sub_args:
        results = await index.search(query=sub_args, limit=10)
        if not results:
            return CommandResult.info("No results.")
        lines = f"## CodeIndex Search ({len(results)} results)\n"
        for i, r in enumerate(results, 1):
            score_str = f"{r.score:.3f}" if r.score is not None else "n/a"
            lines += (
                f"\n**{i}. {r.name}** (`{r.item_id}`)"
                f" — {r.path} (score {score_str})\n"
                f"{r.chunk_preview or ''}\n"
            )
        return CommandResult.markdown(lines)

    if subcommand == "item" and sub_args:
        item = await index.get_item(item_id=sub_args.strip())
        if item is None:
            return CommandResult.error(f"Item {sub_args.strip()} not found.")
        preview = item.content
        if len(preview) > 1500:
            preview = preview[:1500] + "..."
        return CommandResult.markdown(
            f"## {item.name}\n"
            f"- **id:** `{item.item_id}`\n"
            f"- **path:** {item.path}\n"
            f"- **type:** {item.type}\n"
            f"- **commit:** {item.commit}\n\n"
            f"```\n{preview}\n```"
        )

    if subcommand == "commits":
        state = index.manifest.load()
        if not state.commits:
            return CommandResult.info("No commits indexed.")
        lines = f"## Indexed Commits (head: `{state.head or 'none'}`)\n"
        for sha, info in sorted(
            state.commits.items(),
            key=lambda kv: kv[1].last_used_at,
            reverse=True,
        ):
            head_marker = " (HEAD)" if sha == state.head else ""
            branch = f" branches: {', '.join(info.branch_refs)}" if info.branch_refs else ""
            lines += f"\n- `{sha}`{head_marker} — last used {info.last_used_at}{branch}"
        return CommandResult.markdown(lines)

    if subcommand == "clean":
        dropped = await index.clean()
        if not dropped:
            return CommandResult.info("Nothing to clean.")
        return CommandResult.info(f"Dropped {len(dropped)} commit(s): {', '.join(dropped)}")

    if subcommand == "sync":
        target_sha = sub_args or None
        result = await sync.sync_now(sha=target_sha)
        # Re-derive ``codeindex_available`` so the agent's prompt
        # matches the post-sync chroma state.
        try:
            session.refresh_codeindex_availability()
        except Exception as exc:
            logger.debug("refresh after /codeindex sync failed (%s)", exc)
        if result.link_start_url:
            _open_in_browser(result.link_start_url)
            lines = (
                f"### CodeIndex needs setup\n"
                f"{result.reason}\n\n"
                f"Opening your browser to:\n"
                f"`{result.link_start_url}`\n\n"
                f"After the GitHub UI finishes, run `/codeindex sync` again."
            )
            return CommandResult.markdown(lines)
        if result.skipped:
            return CommandResult.info(f"Sync skipped: {result.reason}")
        if result.error:
            return CommandResult.error(
                f"Sync of {result.commit_sha[:8] if result.commit_sha else '?'} failed: {result.error}"
            )
        stats = result.stats
        short_sha = result.commit_sha[:8] if result.commit_sha else "?"
        return CommandResult.info(
            f"Synced {short_sha}: "
            f"{stats.items_upserted} upserts, {stats.items_deleted} deletes, "
            f"{stats.references_upserted} refs."
        )

    if subcommand == "resync":
        # Wipe the local chroma for the target sha and pull a fresh
        # snapshot. Used when the local index drifts from the cloud
        # definition — e.g. an earlier sync took the delta path with
        # an absent parent and stored only the diff's items.
        target_sha = sub_args or await asyncio.to_thread(sync.current_sha)
        if not target_sha:
            return CommandResult.error("Not a git repository — pass an explicit sha.")
        forgot = await index.forget_commit(target_sha)
        result = await sync.sync_now(sha=target_sha, force_snapshot=True)
        try:
            session.refresh_codeindex_availability()
        except Exception as exc:
            logger.debug("refresh after /codeindex resync failed (%s)", exc)
        short_sha = (result.commit_sha or target_sha)[:8]
        if result.skipped:
            prefix = "Wiped local index; " if forgot else ""
            return CommandResult.info(f"{prefix}sync skipped: {result.reason}")
        if result.error:
            return CommandResult.error(f"Resync of {short_sha} failed: {result.error}")
        stats = result.stats
        prefix = "Wiped local index. " if forgot else ""
        return CommandResult.info(
            f"{prefix}Resynced {short_sha} via snapshot: "
            f"{stats.items_upserted} upserts, "
            f"{stats.references_upserted} refs."
        )

    if subcommand == "install":
        # Explicit "open the install page for this repo" entry point —
        # useful when `sync` already succeeded but the user wants to
        # add a sibling repo, or revisit the install screen.
        resolver = sync.resolver
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
        _open_in_browser(resolved.install_url)
        return CommandResult.markdown(
            f"### Install igniIndex\nOpening your browser:\n`{resolved.install_url}`"
        )

    if subcommand == "status":
        # Both helpers shell out to ``git``; offload to a thread so
        # the BE's dispatcher keeps serving other sessions' RPCs.
        local_sha = await asyncio.to_thread(sync.current_sha)
        last = sync.last_synced_sha
        head = index.head()
        remote_url = (
            await asyncio.to_thread(sync.resolver.remote_url) if sync.resolver else None
        )
        resolved = sync.resolver.cached if sync.resolver else None
        lines = "## CodeIndex Status\n"
        lines += f"- local HEAD: `{local_sha or 'not a git repo'}`\n"
        lines += f"- git remote: `{remote_url or 'not a git repo'}`\n"
        lines += f"- last synced: `{last or 'never'}`\n"
        lines += f"- index head: `{head or 'none'}`\n"
        if resolved is None:
            lines += "- discovered: `not yet (run /codeindex sync)`\n"
        elif resolved.needs_install:
            lines += "- discovered: `install required`\n"
            lines += f"- install URL: `{resolved.install_url or 'unavailable'}`\n"
        else:
            lines += f"- discovered: `{resolved.repository_id}`\n"
        return CommandResult.markdown(lines)

    return CommandResult.markdown(
        "## CodeIndex\n"
        "Run `/codeindex` with no args to open the interactive status "
        "panel (current-commit indexed state + sync/clean/install "
        "actions, with a 2s live poll).\n"
        "- `/codeindex search <query>` — semantic search the head commit (chat output)\n"
        "- `/codeindex item <id>` — show full item details in chat\n"
        "- `/codeindex commits` — list indexed commits as markdown\n"
        "- `/codeindex clean` — drop stale, non-branch commits\n"
        "- `/codeindex sync [sha]` — pull and apply a changeset (defaults to HEAD)\n"
        "- `/codeindex resync [sha]` — wipe local state and pull a fresh snapshot\n"
        "- `/codeindex install` — open the GitHub App install page for this repo\n"
        "- `/codeindex status` — show sync state and install progress\n"
    )
