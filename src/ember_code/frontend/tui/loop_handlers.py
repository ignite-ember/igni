"""Loop panel event handlers for :class:`EmberApp`.

Extracted from ``tui/app.py``. Same pattern as
``codeindex_handlers.py``: keep the ``@on(...)``-decorated
class method on :class:`EmberApp` as a one-line delegate, put
the body here.

Five free functions taking ``app: EmberApp`` as first arg:

* :func:`show_loop_panel` — mount + start 1s poll.
* :func:`poll_loop_status` — refresh panel header, skip when
  busy label is up or panel unmounted.
* :func:`on_loop_resume` — unpause + fire the interrupted
  iteration via ``app._controller._run(prompt)`` so the
  cancel guard doesn't see the prompt.
* :func:`on_loop_cancel` — cancel pending loop, refresh
  header.
* :func:`on_loop_panel_closed` — stop poll, restore focus.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ember_code.frontend.tui.widgets import (
    LoopPanelWidget,
    LoopStatusInfo,
    PromptInput,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


async def show_loop_panel(app: "EmberApp") -> None:
    """Open the Loop panel. One status RPC populates the header;
    the 1s background poll keeps the iteration counter and
    active/inactive state fresh while the panel is mounted."""
    status = await app._backend.loop_status()
    panel = LoopPanelWidget(status=status)
    app.mount(panel)
    # Widget self-focuses in ``on_mount`` — see LoopPanelWidget
    # docstring for why a parent-side ``.focus()`` would race
    # with the async mount.
    app._loop_status_poll = app.set_interval(
        app._LOOP_STATUS_POLL_SECONDS,
        app._poll_loop_status,
    )


async def poll_loop_status(app: "EmberApp") -> None:
    """Live-refresh the panel header.

    Skipped when a busy indicator is up (would overwrite the
    spinner mid-RPC) or when the panel has been unmounted
    (race with ``PanelClosed``). Transport hiccups are
    swallowed silently — next tick will retry.
    """
    try:
        panel = app.query_one(LoopPanelWidget)
    except Exception:
        return
    if panel._busy_label:
        return
    try:
        status = await app._backend.loop_status()
    except Exception:
        logger.debug("loop status poll failed", exc_info=True)
        return
    panel.set_status(status)


async def on_loop_resume(app: "EmberApp") -> None:
    """Panel ``R`` key — unpause and re-fire the interrupted
    iteration. Mirrors what ``/loop resume`` does from chat:
    flips the paused flag on the backend, then fires
    ``_run(prompt)`` directly to bypass the cancel guard."""
    try:
        panel = app.query_one(LoopPanelWidget)
    except Exception:
        return
    panel.set_busy("Resuming loop…")
    try:
        prompt = await app._backend.loop_resume()
    finally:
        panel.set_busy(None)
    if not prompt:
        app._conversation.append_info("Nothing to resume.")
        return
    # Refresh the header so the badge flips from paused→running
    # before iteration K starts streaming.
    try:
        status = await app._backend.loop_status()
        panel.set_status(status)
    except Exception:
        pass
    # Fire the interrupted iteration on the FE directly — same
    # path the run_prompt action dispatch takes, so the cancel
    # guard never sees the prompt and the loop continues
    # naturally after this iteration completes.
    asyncio.create_task(app._controller._run(prompt))


async def on_loop_cancel(app: "EmberApp") -> None:
    """Cancel any pending loop and refresh the panel header.
    Refreshes the header even when nothing was cancelled (race:
    loop completed between user press and the RPC reaching the
    backend), so the post-cancel state is visible either way."""
    try:
        panel = app.query_one(LoopPanelWidget)
    except Exception:
        return
    panel.set_busy("Cancelling loop…")
    try:
        cancelled = await app._backend.cancel_pending_loop()
        if cancelled:
            app._conversation.append_info("Loop cancelled.")
        status = await app._backend.loop_status()
        panel.set_status(status)
    finally:
        panel.set_busy(None)


def on_loop_panel_closed(app: "EmberApp") -> None:
    """Stop the background poll and restore focus to the prompt
    input. Without this the 1s interval keeps firing
    ``loop_status`` RPCs after the widget is gone."""
    timer = getattr(app, "_loop_status_poll", None)
    if timer is not None:
        with contextlib.suppress(Exception):
            timer.stop()
        app._loop_status_poll = None
    app.query_one("#user-input", PromptInput).focus()
