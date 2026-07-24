"""Strategy objects for splitting a ``/schedule`` free-text argument
into ``(description, scheduled_at, recurrence)``.

The old procedural :func:`~ember_code.backend.cmd_schedule.ScheduleCommand._schedule_add`
ran two parallel ``for sep in (...)`` loops — one for recurring
patterns (``every``/``daily``/``hourly``/``weekly``), one for one-shot
patterns (``at``/``in``/``on``/``tomorrow``). Each loop duplicated
the ``rfind`` separator search, the description/tail split, the
parser call, and the "did we get a hit?" branch. This module
promotes each loop body onto a :class:`ScheduleClauseParser` subclass
so the coordinator collapses to a single ``for parser in
self._parsers: parsed = parser.try_parse(text)`` iteration.

Both parsers carry their separator tuple as a Pydantic model field
so what used to be a literal in the coordinator becomes data on the
parser object. The concrete pair is ordered:

1. :class:`RecurringClauseParser` — must run first, because
   ``run tests every 2 hours at 5pm`` contains BOTH an ``every``
   marker AND an ``at`` marker. Recurring wins.
2. :class:`OneShotClauseParser` — the fallback for plain
   ``review codebase at 5pm`` / ``audit tomorrow`` phrasings.

Placed under ``core/scheduler/`` so the domain layer owns the
parsing strategy — the backend coordinator only iterates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from ember_code.core.scheduler.parser import parse_time
from ember_code.core.scheduler.recurrence import Recurrence


class ParsedSchedule(BaseModel):
    """Result of a successful ``ScheduleClauseParser.try_parse``.

    ``recurrence`` is the empty string for one-shot tasks so the
    same shape flows into
    :meth:`~ember_code.core.scheduler.models.ScheduledTask.new` /
    :class:`~ember_code.backend.schemas_scheduler.TaskScheduledView`
    regardless of which parser produced the hit.
    """

    description: str
    scheduled_at: datetime
    recurrence: str = ""


class ScheduleClauseParser(Protocol):
    """Strategy protocol — one implementation per phrasing family."""

    def try_parse(self, text: str) -> ParsedSchedule | None:
        """Return a :class:`ParsedSchedule` if ``text`` matches this
        parser's shape, or ``None`` to defer to the next strategy.
        """
        ...


class RecurringClauseParser(BaseModel):
    """Handles recurring phrasings: ``every``/``daily``/``hourly``/``weekly``.

    Matches on any of the recurrence markers via ``rfind`` (right-most
    wins so ``review the daily digest every 2 hours`` splits on
    ``every`` rather than the earlier ``daily``). Delegates the actual
    time math to :meth:`Recurrence.parse`.
    """

    separators: tuple[str, ...] = (" every ", " daily", " hourly", " weekly")

    def try_parse(self, text: str) -> ParsedSchedule | None:
        for sep in self.separators:
            idx = text.lower().rfind(sep)
            if idx > 0:
                description = text[:idx].strip()
                recur_part = text[idx:].strip()
                result = Recurrence.parse(recur_part)
                if result is not None:
                    return ParsedSchedule(
                        description=description,
                        scheduled_at=result.first_scheduled,
                        recurrence=result.recurrence.canonical(),
                    )
        return None


class OneShotClauseParser(BaseModel):
    """Handles one-shot phrasings: ``at``/``in``/``on``/``tomorrow``.

    Right-most separator wins so ``ping the on-call at 5pm`` splits
    on ``at`` rather than the earlier ``on``.
    """

    separators: tuple[str, ...] = (" at ", " in ", " on ", " tomorrow")

    def try_parse(self, text: str) -> ParsedSchedule | None:
        for sep in self.separators:
            idx = text.lower().rfind(sep)
            if idx > 0:
                description = text[:idx].strip()
                time_part = text[idx:].strip()
                scheduled = parse_time(time_part)
                if scheduled:
                    return ParsedSchedule(
                        description=description,
                        scheduled_at=scheduled,
                    )
        return None


__all__ = [
    "ParsedSchedule",
    "ScheduleClauseParser",
    "RecurringClauseParser",
    "OneShotClauseParser",
]
