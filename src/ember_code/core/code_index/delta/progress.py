"""Progress-reporting seam for :class:`DeltaApplier`.

Extracted from the applier so the "never let progress break apply"
invariant lives in one place (was two duplicated try/except blocks
in the old free function). The applier constructs a
:class:`SafeProgressReporter` once at ``__init__`` and calls
:meth:`SafeProgressReporter.report` at each progress point — the
wrapper swallows-and-logs any callback exception so a misbehaving UI
handler can't abort the indexing path.

Formalising this as a class also makes the reporter swap-testable:
inject a recording reporter in tests instead of monkey-patching the
callback signature.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger(__name__)

# Public callback signature: ``(done, total, current_label)``.
ProgressCallback = Callable[[int, int, str], None]


class ProgressReporter(Protocol):
    """Anything that can accept ``(done, total, label)`` triples.

    The applier depends on this Protocol rather than the concrete
    :class:`SafeProgressReporter` so tests can substitute a recording
    reporter without patching module globals.
    """

    def report(self, done: int, total: int, label: str) -> None: ...


class SafeProgressReporter:
    """Wraps a user-supplied callback so exceptions never escape.

    Progress is a UI nicety, not a load-bearing part of the indexing
    path. If the caller's callback raises we log at debug and
    continue — the alternative (letting a UI bug abort a codeindex
    sync) is strictly worse than a missed progress tick.

    A ``None`` callback turns :meth:`report` into a no-op; callers
    can construct a reporter unconditionally instead of branching on
    whether progress was wired.
    """

    def __init__(self, callback: ProgressCallback | None) -> None:
        self._callback = callback

    @property
    def enabled(self) -> bool:
        """True iff a real callback was supplied at construction.

        Callers can use this to skip up-front work that only exists
        to feed the progress bar (e.g. the pre-pass item count).
        """
        return self._callback is not None

    def report(self, done: int, total: int, label: str) -> None:
        if self._callback is None:
            return
        try:
            self._callback(done, total, label)
        except Exception:  # noqa: BLE001 — defensive: never let progress break apply
            logger.debug("on_progress raised; continuing without progress updates")
