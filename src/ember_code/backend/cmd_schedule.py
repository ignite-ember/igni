"""``/schedule`` slash command implementation.

Extracted from :mod:`ember_code.backend.command_handler` — the
scheduling command family. Handles:

* No-arg / ``list`` — open the task panel.
* ``add`` (explicit or implicit via a time-word marker) — parse
  "description at/in/every time" and create a `ScheduledTask`.
* ``rm`` / ``remove`` / ``cancel`` <id> — cancel a pending task.
* ``show`` <id> — render a task's details as markdown.

The implicit-add heuristic (any phrasing containing ``every`` /
``at`` / ``in`` / ``on`` / ``tomorrow`` / ``daily`` / ``hourly``
/ ``weekly`` triggers parse-and-schedule) is what makes ``/schedule
hello task every minute`` Just Work without users learning the
``add`` prefix.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.parser import parse_recurrence, parse_time
from ember_code.core.scheduler.store import TaskStore

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.backend.command_handler import CommandResult

# Word-boundary check for the implicit-add heuristic — matches ``every``
# / ``daily`` / etc. as whole words so we don't false-positive on
# ``in`` inside ``ping`` or ``at`` inside ``format``.
_SCHEDULE_TIME_MARKER_RE = re.compile(
    r"\b(?:every|daily|hourly|weekly|tomorrow|at|in|on)\b",
    re.IGNORECASE,
)


async def cmd_schedule(handler: "CommandHandler", args: str) -> "CommandResult":
    """Handle ``/schedule`` commands: add, list, remove, show."""
    from ember_code.backend.command_handler import CommandResult
    from ember_code.protocol.messages import CommandAction, CommandResultKind

    session = handler._session
    store = TaskStore(project_dir=session.project_dir)
    parts = args.strip().split(None, 1)
    subcommand = parts[0].lower() if parts else "list"
    sub_args = parts[1].strip() if len(parts) > 1 else ""

    # No args or "list" → open the task panel.
    if subcommand == "list" or not args.strip():
        return CommandResult(
            kind=CommandResultKind.INFO, content="", action=CommandAction.SCHEDULE
        )

    if subcommand == "add" and sub_args:
        return await _schedule_add(store, sub_args)

    # Implicit add: any phrasing that contains a time-clause word but
    # doesn't start with a known sub-command. So ``/schedule hello
    # task every minute`` Just Works instead of silently opening the
    # panel. Use word boundaries so we don't false-positive on ``in``
    # inside ``ping`` or ``at`` inside ``format``.
    known_subcommands = {"add", "list", "rm", "remove", "cancel", "show"}
    if subcommand not in known_subcommands:
        raw = args.strip()
        if _SCHEDULE_TIME_MARKER_RE.search(raw):
            return await _schedule_add(store, raw)

    if subcommand in ("rm", "remove", "cancel") and sub_args:
        task_id = sub_args.strip()
        task = await store.get(task_id)
        if not task:
            return CommandResult.error(f"Task not found: {task_id}")
        if task.status in (TaskStatus.pending, TaskStatus.running):
            await store.update_status(task_id, TaskStatus.cancelled)
            return CommandResult.info(f"Cancelled task {task_id}")
        return CommandResult.info(f"Task {task_id} is already {task.status.value}")

    if subcommand == "show" and sub_args:
        task = await store.get(sub_args.strip())
        if not task:
            return CommandResult.error(f"Task not found: {sub_args.strip()}")
        lines = (
            f"## Task {task.id}\n"
            f"- **Description:** {task.description}\n"
            f"- **Scheduled:** {task.scheduled_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"- **Status:** {task.status.value}\n"
            f"- **Created:** {task.created_at.strftime('%Y-%m-%d %H:%M')}\n"
        )
        if task.result:
            lines += f"\n**Result:**\n{task.result}\n"
        if task.error:
            lines += f"\n**Error:**\n{task.error}\n"
        return CommandResult.markdown(lines)

    # Unknown subcommand — open the panel.
    return CommandResult(
        kind=CommandResultKind.INFO, content="", action=CommandAction.SCHEDULE
    )


async def _schedule_add(store, text: str) -> "CommandResult":
    """Parse "description at/in/every time" and create a task."""
    from ember_code.backend.command_handler import CommandResult
    from ember_code.protocol.messages import CommandAction, CommandResultKind

    # Try recurring: "run tests every 2 hours", "check deps daily",
    # "audit weekly at 9am".
    for sep in (" every ", " daily", " hourly", " weekly"):
        idx = text.lower().rfind(sep)
        if idx > 0:
            description = text[:idx].strip()
            recur_part = text[idx:].strip()
            recurrence, scheduled = parse_recurrence(recur_part)
            if recurrence and scheduled:
                task = ScheduledTask(
                    id=uuid.uuid4().hex[:8],
                    description=description,
                    scheduled_at=scheduled,
                    recurrence=recurrence,
                )
                await store.add(task)
                return CommandResult(
                    kind=CommandResultKind.INFO,
                    content=(
                        f'Scheduled `{task.id}`: "{description}" '
                        f"({recurrence}, first at {scheduled.strftime('%Y-%m-%d %H:%M')})"
                    ),
                    action=CommandAction.SCHEDULE,
                )

    # Try one-shot: "review codebase at 5pm".
    for sep in (" at ", " in ", " on ", " tomorrow"):
        idx = text.lower().rfind(sep)
        if idx > 0:
            description = text[:idx].strip()
            time_part = text[idx:].strip()
            scheduled = parse_time(time_part)
            if scheduled:
                task = ScheduledTask(
                    id=uuid.uuid4().hex[:8],
                    description=description,
                    scheduled_at=scheduled,
                )
                await store.add(task)
                return CommandResult(
                    kind=CommandResultKind.INFO,
                    content=(
                        f'Scheduled `{task.id}`: "{description}" '
                        f"at {scheduled.strftime('%Y-%m-%d %H:%M')}"
                    ),
                    action=CommandAction.SCHEDULE,
                )

    return CommandResult.error(
        "Could not parse the time clause. The `add` prefix is "
        "optional — any phrasing that contains `at`, `in`, `on`, "
        "`tomorrow`, `every`, `daily`, `hourly`, or `weekly` works.\n\n"
        "Examples:\n"
        "  /schedule review the codebase at 5pm\n"
        "  /schedule run tests in 30 minutes\n"
        "  /schedule audit security tomorrow\n"
        "  /schedule run tests every 2 hours\n"
        "  /schedule check dependencies daily"
    )
