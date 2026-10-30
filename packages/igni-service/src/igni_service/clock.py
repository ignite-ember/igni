"""Time, as one object, so tests can move it.

Two readings because they answer different questions: `wall` is what a
token's `exp` is compared with, `monotonic` is what a cache's age is
measured in — and a cache must not expire because somebody set the
machine's clock back.
"""

from __future__ import annotations

import time


class Clock:
    """The system's clocks."""

    def wall(self) -> float:
        """Seconds since the epoch."""
        return time.time()

    def monotonic(self) -> float:
        """Seconds on a clock that never goes back."""
        return time.monotonic()
