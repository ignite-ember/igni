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

Coordinator surface: :class:`ScheduleCommand` owns every verb via
a :attr:`_VERBS` bound-method table plus an implicit-add fallback.
Presentation lives in :mod:`schemas_scheduler`; the parsing
strategy lives on
:class:`~ember_code.core.scheduler.clause_parsers.ScheduleClauseParser`
implementations. The public :func:`cmd_schedule` is a two-line
shim so :mod:`ember_code.backend.command_handler` keeps importing
it by name.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, ClassVar

from ember_code.backend.command_result import CommandResult
from ember_code.backend.schemas_scheduler import (
    ScheduleUsageView,
    TaskAlreadyDoneView,
    TaskCancelledView,
    TaskDetailsView,
    TaskNotFoundView,
    TaskScheduledView,
)
from ember_code.core.scheduler.clause_parsers import (
    OneShotClauseParser,
    ParsedSchedule,
    RecurringClauseParser,
    ScheduleClauseParser,
)
from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.store import TaskStore
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session

# Word-boundary check for the implicit-add heuristic — matches ``every``
# / ``daily`` / etc. as whole words so we don't false-positive on
# ``in`` inside ``ping`` or ``at`` inside ``format``.
_SCHEDULE_TIME_MARKER_RE = re.compile(
    r"\b(?:every|daily|hourly|weekly|tomorrow|at|in|on)\b",
    re.IGNORECASE,
)


class ScheduleCommand:
    """Coordinator for the ``/schedule`` slash-command family.

    Every verb is a bound method registered in :attr:`_VERBS`; the
    implicit-add fallback is :meth:`_try_implicit_add`. Parsing is
    delegated to an ordered tuple of
    :class:`ScheduleClauseParser` strategies (recurring before
    one-shot — see :meth:`__init__` for the ordering invariant).
    Presentation is delegated to view models in
    :mod:`schemas_scheduler`.
    """

    # Bound-method verb table — populated after the class body to
    # sidestep the forward-reference / ``Self`` issue that mypy
    # flags on inline dispatch tables. See ``_VERBS = { ... }``
    # assignment below.
    _VERBS: ClassVar[dict[str, Callable[..., Awaitable[CommandResult]]]]

    def __init__(self, session: Session) -> None:
        self._session = session
        self._store = TaskStore(project_dir=session.project_dir)
        # ORDERING INVARIANT: recurring first, one-shot second. The
        # phrase ``run tests every 2 hours at 5pm`` contains BOTH
        # ``every`` and ``at``; swapping this tuple would misroute
        # to the one-shot branch and drop the recurrence.
        self._parsers: tuple[ScheduleClauseParser, ...] = (
            RecurringClauseParser(),
            OneShotClauseParser(),
        )

    async def dispatch(self, args: str) -> CommandResult:
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else "list"
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        # No args → open the task panel.
        if not args.strip():
            return CommandResult.for_action(CommandAction.SCHEDULE)

        verb = self._VERBS.get(subcommand)
        if verb is not None:
            return await verb(self, sub_args)

        # Unknown leading token → try the implicit-add heuristic on
        # the full arg string. ``/schedule hello task every minute``
        # should Just Work instead of silently opening the panel.
        return await self._try_implicit_add(args.strip())

    # ── Verb methods ─────────────────────────────────────────────

    async def _list(self, _sub_args: str) -> CommandResult:
        """``/schedule list`` — open the task panel."""
        return CommandResult.for_action(CommandAction.SCHEDULE)

    async def _add(self, sub_args: str) -> CommandResult:
        """``/schedule add <phrase>`` — parse + create a task."""
        if not sub_args:
            return await self._try_implicit_add("")
        return await self._schedule_add(sub_args)

    async def _cancel_verb(self, sub_args: str) -> CommandResult:
        """``/schedule rm``/``remove``/``cancel`` <id>."""
        if not sub_args:
            return CommandResult.for_action(CommandAction.SCHEDULE)
        return await self._cancel(sub_args.strip())

    async def _show_verb(self, sub_args: str) -> CommandResult:
        """``/schedule show`` <id>."""
        if not sub_args:
            return CommandResult.for_action(CommandAction.SCHEDULE)
        return await self._show(sub_args.strip())

    # ── Implementation helpers ───────────────────────────────────

    async def _try_implicit_add(self, raw: str) -> CommandResult:
        """Route unknown leading tokens through the parse-and-schedule
        heuristic. Falls back to opening the panel when the phrase
        doesn't contain any time marker."""
        if raw and _SCHEDULE_TIME_MARKER_RE.search(raw):
            return await self._schedule_add(raw)
        return CommandResult.for_action(CommandAction.SCHEDULE)

    async def _cancel(self, task_id: str) -> CommandResult:
        task = await self._store.get(task_id)
        if not task:
            return TaskNotFoundView(task_id=task_id).to_command_result()
        if task.status.is_active:
            await self._store.update_status(task_id, TaskStatus.cancelled)
            return TaskCancelledView(task_id=task_id).to_command_result()
        return TaskAlreadyDoneView(task_id=task_id, status=task.status).to_command_result()

    async def _show(self, task_id: str) -> CommandResult:
        task = await self._store.get(task_id)
        if not task:
            return TaskNotFoundView(task_id=task_id).to_command_result()
        return TaskDetailsView(task=task).to_command_result()

    async def _schedule_add(self, text: str) -> CommandResult:
        """Parse "description at/in/every time" and create a task.

        Iterates over the ordered :attr:`_parsers` tuple; the first
        parser that returns a :class:`ParsedSchedule` wins. Falls
        back to :class:`ScheduleUsageView` when nothing matches.
        """
        for parser in self._parsers:
            parsed = parser.try_parse(text)
            if parsed is not None:
                return await self._create_scheduled_task(parsed)
        return ScheduleUsageView.to_command_result()

    async def _create_scheduled_task(self, parsed: ParsedSchedule) -> CommandResult:
        """Persist a task from a :class:`ParsedSchedule` and render the
        success view."""
        task = ScheduledTask.new(
            description=parsed.description,
            scheduled_at=parsed.scheduled_at,
            recurrence=parsed.recurrence,
        )
        await self._store.add(task)
        return TaskScheduledView(
            task_id=task.id,
            description=parsed.description,
            scheduled_at=parsed.scheduled_at.strftime("%Y-%m-%d %H:%M"),
            recurrence=parsed.recurrence,
        ).to_command_result()


# Populate the verb table AFTER the class body so bound-method
# forward references resolve cleanly (and mypy has proper access
# to the ``Self``-typed methods above).
ScheduleCommand._VERBS = {
    "list": ScheduleCommand._list,
    "add": ScheduleCommand._add,
    "rm": ScheduleCommand._cancel_verb,
    "remove": ScheduleCommand._cancel_verb,
    "cancel": ScheduleCommand._cancel_verb,
    "show": ScheduleCommand._show_verb,
}


async def cmd_schedule(handler: CommandHandler, args: str) -> CommandResult:
    """See :meth:`ScheduleCommand.dispatch`."""
    return await ScheduleCommand(handler.session).dispatch(args)
