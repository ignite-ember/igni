"""Scheduler + task panel + queue panel handlers for :class:`EmberApp`.

Extracted from ``tui/app.py``. Kept together because all three
concern "deferred work the agent is executing on the user's
behalf" — queue is the FIFO of drafts, task panel is the
scheduled job list, scheduler is the background poller.

Free functions taking ``app: EmberApp`` as first arg:

* Queue: :func:`on_queue_item_deleted`,
  :func:`on_queue_item_edit`, :func:`on_queue_panel_closed`.
* Task panel: :func:`on_task_cancelled`,
  :func:`on_task_panel_closed`.
* Scheduler: :func:`start_scheduler`,
  :func:`execute_scheduled_task`,
  :func:`on_scheduled_task_started`,
  :func:`on_scheduled_task_completed`,
  :func:`refresh_task_panel`.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from textual.css.query import NoMatches
from textual.widgets import Static

from ember_code.frontend.tui.widgets import (
    PromptInput,
    QueuePanel,
    TaskPanel,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


# ── Queue panel ───────────────────────────────────────────────


def on_queue_item_deleted(app: "EmberApp", index: int) -> None:
    """Dequeue by index; announce the removed text (truncated)."""
    removed = app._controller.dequeue_at(index)
    if removed:
        short = removed if len(removed) <= 40 else removed[:37] + "..."
        app._conversation.append_info(f"Removed from queue: {short}")


def on_queue_item_edit(app: "EmberApp", index: int, text: str) -> None:
    """Remove the item from the queue and put its text into the
    input box for editing."""
    app._controller.dequeue_at(index)
    input_widget = app.query_one("#user-input", PromptInput)
    input_widget.clear()
    input_widget.insert(text)
    input_widget.focus()


def on_queue_panel_closed(app: "EmberApp") -> None:
    """Hide the queue panel and restore prompt focus."""
    with contextlib.suppress(NoMatches):
        app.query_one("#queue-panel", QueuePanel).add_class("-hidden")
    app.query_one("#user-input", PromptInput).focus()


# ── Task panel ────────────────────────────────────────────────


async def on_task_cancelled(app: "EmberApp", task_id: str) -> None:
    """Cancel a scheduled task, log the result, refresh the panel."""
    result = await app._backend.cancel_scheduled_task(task_id)
    app._conversation.append_info(result.text)
    await refresh_task_panel(app)


def on_task_panel_closed(app: "EmberApp") -> None:
    """Hide the task panel, stop the 1s auto-refresh interval if
    active, restore prompt focus."""
    with contextlib.suppress(NoMatches):
        app.query_one("#task-panel", TaskPanel).add_class("-hidden")
    if hasattr(app, "_task_refresh_timer") and app._task_refresh_timer:
        app._task_refresh_timer.stop()
        app._task_refresh_timer = None
    app.query_one("#user-input", PromptInput).focus()


async def refresh_task_panel(app: "EmberApp") -> None:
    """Refresh the task panel with the current scheduled-task
    list via the backend."""
    try:
        tasks = await app._backend.get_scheduled_tasks(include_done=True)
        panel = app.query_one("#task-panel", TaskPanel)
        panel.refresh_tasks(tasks)
    except Exception:
        pass


# ── Scheduler ─────────────────────────────────────────────────


def start_scheduler(app: "EmberApp") -> None:
    """Start the background scheduler via backend. Called once
    from ``on_mount_inner`` after the BE is up."""
    app._scheduler_runner = app._backend.start_scheduler(
        on_task_started=app._on_scheduled_task_started,
        on_task_completed=app._on_scheduled_task_completed,
    )


async def execute_scheduled_task(app: "EmberApp", description: str) -> str:
    """Execute a scheduled task through the backend. Never
    raises — returns the error string so the scheduler runner
    can log it."""
    try:
        return await app._backend.execute_scheduled_task(description)
    except Exception as exc:
        return f"Error: {exc}"


def on_scheduled_task_started(
    app: "EmberApp",
    task_id: str,
    description: str,
) -> None:
    """Announce a task-started event in the conversation and via
    Textual notify. Also fires a task-panel refresh."""
    short = description[:50] + ("..." if len(description) > 50 else "")
    app._conversation.append_info(f"⚡ Running scheduled task `{task_id}`: {short}")
    app.notify(f"Task {task_id} started: {short}", title="Scheduler", timeout=5)
    asyncio.create_task(refresh_task_panel(app))


def on_scheduled_task_completed(
    app: "EmberApp",
    task_id: str,
    description: str,
    success: bool,
) -> None:
    """Announce a task-completed event in the conversation +
    notify + refresh. Split success / failure into distinct
    label + severity paths."""
    short = description[:50] + ("..." if len(description) > 50 else "")
    if success:
        app._conversation.append(
            Static(
                f"[green]✓[/green] Task `{task_id}` completed: {short}"
                f"  [dim]→ /schedule show {task_id}[/dim]",
                classes="task-event",
            )
        )
        app.notify(
            f"Task {task_id} completed: {short}",
            title="Scheduler",
            severity="information",
            timeout=8,
        )
    else:
        app._conversation.append(
            Static(
                f"[red]✗[/red] Task `{task_id}` failed: {short}"
                f"  [dim]→ /schedule show {task_id}[/dim]",
                classes="task-event",
            )
        )
        app.notify(
            f"Task {task_id} failed: {short}",
            title="Scheduler",
            severity="error",
            timeout=10,
        )
    asyncio.create_task(refresh_task_panel(app))
