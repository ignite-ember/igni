"""Natural language time parser for scheduling.

Narrow-purpose leaf module: exposes a single ``parse_time`` free
function that takes a primitive string and returns a :class:`datetime`
(or ``None``). It is kept as a free function on purpose — it has no
shared implicit subject with any other helper (Rule 6 stateless-leaf
exception), and wrapping a one-line ``dateparser.parse`` call in a
class would be pure ceremony.

Recurrence parsing (``every 2 hours``, ``daily``, …) previously lived
here as free functions too. It now lives on the
:class:`~ember_code.core.scheduler.recurrence.Recurrence` value object
in the sibling ``recurrence.py`` module — an ``(amount, unit)`` pair
IS an object with behaviour, so callers use
``Recurrence.parse(...).next_after(...)`` / ``.canonical()`` instead
of the old free-function tuple protocol.

Uses ``dateparser`` for multilingual support (200+ languages).

Supports formats like:
- "in 5 minutes", "через 5 хвилин", "en 30 minutos"
- "at 5pm", "о 17:00", "a las 5pm"
- "tomorrow", "завтра", "mañana"
- "tomorrow at 9am", "завтра о 9 ранку"
- "2026-03-20 14:00"
"""

import re
from datetime import datetime, timedelta

import dateparser


def parse_time(text: str) -> datetime | None:
    """Parse a natural language time expression into a datetime.

    Uses dateparser for multilingual support. Falls back to None
    if the text can't be parsed.
    """
    text = text.strip()

    result = dateparser.parse(
        text,
        settings={
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )

    if result is None:
        return None

    # If the parsed time is in the past, push to tomorrow (for "at 5pm" style)
    is_explicit_date = re.search(r"\d{4}[-/]", text) or "tomorrow" in text.lower()
    if result <= datetime.now() and not is_explicit_date:
        result += timedelta(days=1)

    return result
