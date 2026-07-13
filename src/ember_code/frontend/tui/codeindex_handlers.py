"""CodeIndex panel event handlers for :class:`EmberApp`.

Extracted from ``tui/app.py``. The Textual ``@on(...)``
decorator has to sit on class methods (the framework wires
event dispatch by scanning the class), so each handler on
:class:`EmberApp` stays as a one-line delegate to the free
function here.

Six free functions taking ``app: EmberApp`` as first arg:

* :func:`show_codeindex_panel` — mount the panel, kick the
  background poll.
* :func:`poll_codeindex_status` — 2 s tick that refreshes the
  panel header + the always-on status-bar badge.
* :func:`on_codeindex_sync` — delta pull.
* :func:`on_codeindex_resync` — snapshot re-pull with live
  0.5 s apply-progress polling so the busy label updates
  during the ~30-90 s embed phase.
* :func:`on_codeindex_clean` — retention drop.
* :func:`on_codeindex_install` — open the portal repositories
  page in the browser.
* :func:`on_codeindex_panel_closed` — stop the background
  poll and restore focus to the prompt.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import webbrowser
from typing import TYPE_CHECKING

from ember_code.frontend.tui.widgets import (
    CodeIndexPanelWidget,
    PromptInput,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


async def show_codeindex_panel(app: "EmberApp") -> None:
    """Open the CodeIndex panel. One status RPC populates the
    header; the 2s background poll keeps the indexed-state /
    sync-% display fresh while the panel is mounted. There is
    no implicit search RPC on open — the panel is a status
    display, not a query surface."""
    status = await app._backend.codeindex_status()
    panel = CodeIndexPanelWidget(status=status)
    app.mount(panel)
    # The widget self-focuses in ``on_mount`` — it has no
    # focusable children (no Input), so a parent-side
    # ``panel.focus()`` here would race with the async mount.
    # Start the live status poll. Stored on ``app`` so the
    # ``PanelClosed`` handler can stop it — leaking the interval
    # would keep pinging the backend after the panel disappears.
    app._codeindex_status_poll = app.set_interval(
        app._CODEINDEX_STATUS_POLL_SECONDS,
        app._poll_codeindex_status,
    )


async def poll_codeindex_status(app: "EmberApp") -> None:
    """Live-refresh the panel header.

    Skipped when a busy indicator is up — overwriting the busy
    label mid-RPC would erase the spinner the user is watching.
    Also skipped when the panel has been unmounted (race with
    ``PanelClosed``).
    """
    try:
        panel = app.query_one(CodeIndexPanelWidget)
    except Exception:
        return
    if panel._busy_label:
        return
    try:
        status = await app._backend.codeindex_status()
    except Exception:
        # Transport hiccup mid-poll shouldn't kill the interval
        # — next tick will retry. Logged at debug; not surfaced
        # to the user since this is a background refresh.
        logger.debug("codeindex status poll failed", exc_info=True)
        return
    panel.set_status(status)
    # While the panel is open the panel-poll is tighter than
    # the always-on status-bar poll (2s vs 5s) — feed the same
    # snapshot through so the badge updates at the panel's
    # cadence instead of waiting on its own slower tick.
    app._status.set_codeindex_status(status)


async def on_codeindex_sync(app: "EmberApp") -> None:
    """Delta-pull the codeindex to catch up with HEAD."""
    try:
        panel = app.query_one(CodeIndexPanelWidget)
    except Exception:
        return
    panel.set_busy("Syncing changeset…")
    try:
        result = await app._backend.codeindex_sync(None)
        # Surface outcome to the conversation so the user has a
        # durable log line of every sync — the panel header
        # refresh below only reflects the new state.
        if result.get("link_start_url"):
            webbrowser.open(result["link_start_url"])
            app._conversation.append_info(
                f"CodeIndex needs setup. Opened {result['link_start_url']} in your browser. "
                "Re-run sync after finishing the install."
            )
        elif result.get("error"):
            app._conversation.append_error(f"Sync failed: {result['error']}")
        elif result.get("skipped"):
            app._conversation.append_info(
                f"Sync skipped: {result.get('reason', '')}".rstrip(": ")
            )
        else:
            sha = (result.get("commit_sha") or "")[:8]
            app._conversation.append_info(
                f"Synced {sha}: {result.get('items_upserted', 0)} upserts, "
                f"{result.get('items_deleted', 0)} deletes, "
                f"{result.get('references_upserted', 0)} refs."
            )
        # Refresh header — head + commit count likely moved.
        status = await app._backend.codeindex_status()
        panel.set_status(status)
        # Push the same snapshot through to the always-on
        # status-bar slot so the user doesn't wait up to 5s for
        # the next background tick to reflect the new state.
        app._status.set_codeindex_status(status)
    finally:
        panel.set_busy(None)


async def on_codeindex_resync(app: "EmberApp") -> None:
    """Wipe local chroma for HEAD and pull a fresh snapshot.

    Recovery path for indexes that have drifted from the cloud
    definition — same backend behaviour as ``/codeindex resync``.
    The apply phase embeds every chunk through
    sentence-transformers which can run for ~30-90s on a fresh
    checkout; we poll the backend's apply-progress fields
    twice a second and rewrite the busy label so the UI
    doesn't look frozen during that window.
    """
    try:
        panel = app.query_one(CodeIndexPanelWidget)
    except Exception:
        return

    panel.set_busy("Resyncing (full snapshot)…")

    async def _poll_progress() -> None:
        while True:
            try:
                await asyncio.sleep(0.5)
                status = await app._backend.codeindex_status()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue  # transient RPC blips shouldn't kill the ticker
            done = int(status.get("apply_done") or 0)
            total = int(status.get("apply_total") or 0)
            step = (status.get("apply_step") or "").strip()
            if total <= 0:
                continue  # apply hasn't started counting yet
            pct = int(done * 100 / total)
            label = f"Resyncing {pct}% — {done}/{total} items"
            if step:
                short = step if len(step) <= 40 else step[:37] + "…"
                label += f" · {short}"
            panel.set_busy(label)

    ticker = asyncio.create_task(_poll_progress())
    try:
        result = await app._backend.codeindex_resync(None)
        if result.get("link_start_url"):
            webbrowser.open(result["link_start_url"])
            app._conversation.append_info(
                f"CodeIndex needs setup. Opened {result['link_start_url']} in your browser. "
                "Re-run resync after finishing the install."
            )
        elif result.get("error"):
            app._conversation.append_error(f"Resync failed: {result['error']}")
        elif result.get("skipped"):
            app._conversation.append_info(
                f"Resync skipped: {result.get('reason', '')}".rstrip(": ")
            )
        else:
            sha = (result.get("commit_sha") or "")[:8]
            prefix = "Wiped local index. " if result.get("forgot") else ""
            app._conversation.append_info(
                f"{prefix}Resynced {sha} via snapshot: "
                f"{result.get('items_upserted', 0)} upserts, "
                f"{result.get('references_upserted', 0)} refs."
            )
        status = await app._backend.codeindex_status()
        panel.set_status(status)
        app._status.set_codeindex_status(status)
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ticker
        panel.set_busy(None)


async def on_codeindex_clean(app: "EmberApp") -> None:
    """Drop commits past retention rules."""
    try:
        panel = app.query_one(CodeIndexPanelWidget)
    except Exception:
        return
    panel.set_busy("Cleaning…")
    try:
        dropped = await app._backend.codeindex_clean()
        if dropped:
            app._conversation.append_info(
                f"Dropped {len(dropped)} commit(s): {', '.join(dropped)}"
            )
        else:
            app._conversation.append_info("Nothing to clean.")
        status = await app._backend.codeindex_status()
        panel.set_status(status)
        app._status.set_codeindex_status(status)
    finally:
        panel.set_busy(None)


async def on_codeindex_install(app: "EmberApp") -> None:
    """Open the Ember portal's repositories page in the browser.

    The portal page is the canonical entry point for adding a
    repo to CodeIndex — its ``Add repository`` button drives
    the GitHub-App install flow. No status refresh after the
    open; the 2s background poll picks up any state change
    once the user finishes the portal flow.
    """
    try:
        panel = app.query_one(CodeIndexPanelWidget)
    except Exception:
        return
    url = await app._backend.codeindex_install()
    if not url:
        app._conversation.append_error("No portal URL available.")
        return
    webbrowser.open(url)
    app._conversation.append_info(f"Opening {url}")
    # Best-effort refresh of the header — the poll would catch
    # this in ~2s anyway, but doing it now keeps the install
    # state column from looking stale right after the click.
    try:
        status = await app._backend.codeindex_status()
        panel.set_status(status)
        app._status.set_codeindex_status(status)
    except Exception:
        pass


def on_codeindex_panel_closed(app: "EmberApp") -> None:
    """Stop the background poll started in
    :func:`show_codeindex_panel` and restore focus to the
    prompt input."""
    timer = getattr(app, "_codeindex_status_poll", None)
    if timer is not None:
        with contextlib.suppress(Exception):
            timer.stop()
        app._codeindex_status_poll = None
    app.query_one("#user-input", PromptInput).focus()
