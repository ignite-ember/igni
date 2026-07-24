"""Recurrence value object for the scheduler.

Splits off from ``parser.py`` because every recurrence-related helper
that used to live there shares the same implicit subject — an
``(amount, unit)`` pair with a canonical string form. Collecting those
helpers on a class named after that subject collapses:

* The five free functions (``parse_recurrence``,
  ``next_occurrence_from_recurrence``, ``_next_occurrence``,
  ``_recurrence_to_delta``, ``_normalize_unit``) into methods on a
  single :class:`Recurrence` value object.
* The module-level ``_RECURRENCE_ALIASES`` dispatch dict into a
  :class:`Recurrence._ALIASES` classvar owned by the class that
  consumes it.
* The twin ``unit.startswith(...)`` if-chains (one in
  ``_recurrence_to_delta``, one in ``_normalize_unit``) into a single
  :class:`RecurrenceUnit` enum.
* The anonymous ``tuple[str, datetime | None]`` return of
  ``parse_recurrence`` into a named :class:`RecurrenceParseResult`
  Pydantic model (Result-over-sentinel).

Callers persist the canonical string form
(``ScheduledTask.recurrence: str``) so the DB layer stays untouched,
but the in-memory operations all go through :class:`Recurrence`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, Field

from ember_code.core.scheduler.parser import parse_time


class RecurrenceUnit(str, Enum):
    """Canonical time units for a :class:`Recurrence` pattern.

    Replaces the twin ``startswith("min")`` / ``startswith("hour")``
    if-chains that used to live in ``_recurrence_to_delta`` and
    ``_normalize_unit`` with a single enum whose ``.value`` is the
    canonical plural form.
    """

    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"
    WEEKS = "weeks"

    @classmethod
    def from_token(cls, token: str) -> RecurrenceUnit | None:
        """Map a user-typed unit token (``min``, ``minute``, ``hours``…) to the enum."""
        t = token.lower()
        if t.startswith("min"):
            return cls.MINUTES
        if t.startswith("hour"):
            return cls.HOURS
        if t.startswith("day"):
            return cls.DAYS
        if t.startswith("week"):
            return cls.WEEKS
        return None


# Recognised unit tokens for the ``every N units`` regex — the enum's
# ``from_token`` classmethod is the sole normalisation site.
_UNIT_PATTERN = r"min(?:ute)?s?|hours?|days?|weeks?"

# Canonical recurrence string produced by ``Recurrence.canonical`` /
# consumed by ``Recurrence.from_canonical``. Kept module-level as it is
# a regex constant, not mutable state.
_CANONICAL_PATTERN = re.compile(r"every\s+(\d+)\s+(minutes?|hours?|days?|weeks?)")
_PHRASE_PATTERN = re.compile(rf"every\s+(?:(\d+)\s+)?({_UNIT_PATTERN})")


class Recurrence(BaseModel):
    """A recurrence pattern — ``every N <unit>``.

    Owns every operation that used to be a free function in
    ``parser.py``:

    * :meth:`parse` — parse a user phrase (``every 30 minutes``,
      ``daily``, ``daily at 9am``) into a :class:`RecurrenceParseResult`.
    * :meth:`from_canonical` — parse the persisted canonical string
      (``every 1 days``) back into a :class:`Recurrence` for callers
      that only have the on-disk representation.
    * :meth:`to_delta` — the ``(amount, unit)`` → :class:`timedelta` map.
    * :meth:`canonical` — the on-the-wire string form persisted in
      ``ScheduledTask.recurrence``.
    * :meth:`next_after` — the next occurrence after a given anchor.
    * :meth:`first_occurrence` — the first occurrence from a wall-clock
      ``now`` (default :func:`datetime.now`, injected for testability).
    """

    amount: int = Field(ge=1, description="Positive count of units per period.")
    unit: RecurrenceUnit

    # Alias table — the phrases that expand into a fixed ``(amount, unit)``.
    # Classvar owned by the class that consumes it (replaces the
    # module-level ``_RECURRENCE_ALIASES`` dispatch dict).
    _ALIASES: ClassVar[dict[str, tuple[int, RecurrenceUnit]]] = {
        "hourly": (1, RecurrenceUnit.HOURS),
        "daily": (1, RecurrenceUnit.DAYS),
        "weekly": (7, RecurrenceUnit.DAYS),
    }

    # (amount) → timedelta dispatch — enum polymorphism via a callable
    # table so we don't grow yet another ``if unit == …`` chain.
    _UNIT_DELTA: ClassVar[dict[RecurrenceUnit, Callable[[int], timedelta]]] = {
        RecurrenceUnit.MINUTES: lambda n: timedelta(minutes=n),
        RecurrenceUnit.HOURS: lambda n: timedelta(hours=n),
        RecurrenceUnit.DAYS: lambda n: timedelta(days=n),
        RecurrenceUnit.WEEKS: lambda n: timedelta(weeks=n),
    }

    # ── Construction ──────────────────────────────────────────────

    @classmethod
    def parse(cls, text: str) -> RecurrenceParseResult | None:
        """Parse a natural-language recurrence phrase.

        Args:
            text: e.g. ``"every 30 minutes"``, ``"daily"``,
                ``"daily at 9am"``, ``"hourly"``.

        Returns:
            A :class:`RecurrenceParseResult` on a hit, or ``None`` when
            ``text`` is not a recurrence phrase or names an unknown
            unit. Result-over-sentinel — callers no longer unpack an
            anonymous ``("", None)`` tuple.
        """
        text = text.strip().lower()

        # 1) Fixed aliases (``daily`` / ``hourly`` / ``weekly``), optionally
        #    followed by ``at <time>`` to pin the first-occurrence clock.
        for alias, (amount, unit) in cls._ALIASES.items():
            if text.startswith(alias):
                recurrence = cls(amount=amount, unit=unit)
                rest = text[len(alias) :].strip()
                first = recurrence._first_from_suffix(rest)
                if first is not None:
                    return RecurrenceParseResult(recurrence=recurrence, first_scheduled=first)

        # 2) ``every N units`` — N defaults to 1 so ``every minute`` works.
        m = _PHRASE_PATTERN.match(text)
        if m:
            amount = int(m.group(1)) if m.group(1) else 1
            unit = RecurrenceUnit.from_token(m.group(2))
            if unit is None:
                return None
            recurrence = cls(amount=amount, unit=unit)
            rest = text[m.end() :].strip()
            first = recurrence._first_from_suffix(rest)
            if first is not None:
                return RecurrenceParseResult(recurrence=recurrence, first_scheduled=first)

        return None

    @classmethod
    def from_canonical(cls, canonical: str) -> Recurrence | None:
        """Parse a persisted canonical string (``every 1 days``) back into a value object.

        Returns ``None`` for the empty string or malformed input — the
        empty string is the one-shot sentinel on
        ``ScheduledTask.recurrence`` and must round-trip cleanly.
        """
        if not canonical:
            return None
        m = _CANONICAL_PATTERN.fullmatch(canonical.strip())
        if not m:
            return None
        amount = int(m.group(1))
        if amount < 1:
            return None
        unit = RecurrenceUnit.from_token(m.group(2))
        if unit is None:
            return None
        return cls(amount=amount, unit=unit)

    # ── Behaviour ─────────────────────────────────────────────────

    def to_delta(self) -> timedelta:
        """The ``(amount, unit)`` → :class:`timedelta` conversion."""
        return self._UNIT_DELTA[self.unit](self.amount)

    def canonical(self) -> str:
        """Canonical on-the-wire string form persisted in ``ScheduledTask.recurrence``."""
        return f"every {self.amount} {self.unit.value}"

    def next_after(self, anchor: datetime) -> datetime:
        """The next occurrence after ``anchor`` (typically the last run's ``scheduled_at``)."""
        return anchor + self.to_delta()

    def first_occurrence(self, now: datetime | None = None) -> datetime:
        """The first occurrence relative to ``now`` (default: wall-clock now).

        ``now`` is injectable so tests don't have to freeze the clock
        with :mod:`freezegun` just to assert relative deltas.
        """
        return (now or datetime.now()) + self.to_delta()

    # ── Internal helpers ──────────────────────────────────────────

    def _first_from_suffix(self, rest: str) -> datetime | None:
        """Compute the first-scheduled datetime given the tail after the recurrence phrase.

        ``rest`` is either ``""`` (schedule one period from now) or
        ``"at <time>"`` (schedule at the requested clock time).
        """
        if not rest:
            return self.first_occurrence()
        if rest.startswith("at"):
            return parse_time(rest)
        # Anything else after the recurrence phrase is unrecognised —
        # treat as no hit so the caller can surface a parse error.
        return None


class RecurrenceParseResult(BaseModel):
    """Successful :meth:`Recurrence.parse` outcome.

    Replaces the anonymous ``tuple[str, datetime | None]`` return of
    the old ``parse_recurrence`` so callers get named fields and a
    typed :class:`Recurrence` handle instead of the canonical string.
    """

    recurrence: Recurrence
    first_scheduled: datetime


__all__ = ["Recurrence", "RecurrenceParseResult", "RecurrenceUnit"]
