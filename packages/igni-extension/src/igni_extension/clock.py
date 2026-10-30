"""Time, as one object, so tests can move it."""

from __future__ import annotations

import time


class Clock:
    """The system's monotonic clock: a token's age is measured on it."""

    def monotonic(self) -> float:
        return time.monotonic()
