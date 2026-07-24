"""In-progress retry ledger for the HEAD watcher.

Extracted from two ``_``-prefixed fields (``_in_progress_sha``,
``_next_retry_at``) plus a module-level constant
(``IN_PROGRESS_RETRY_SECONDS``) on the old
:class:`CodeIndexSyncManager`. Now a dedicated class shared by
the manager (for reading the ``in_progress_sha`` in
:class:`SyncProgressSnapshot`) and the watcher (for the
retrigger decision).

Retries are flat (no exponential backoff) — indexing rarely
finishes faster than the cadence and a steady interval is
easier to reason about and surface in the panel.
"""

from __future__ import annotations


class InProgressRetryLedger:
    """Tracks the one ``in_progress`` sha we're currently polling.

    Scoped to a single sha at a time — cleared whenever the
    server returns any non-in-progress status, or the watcher
    detects HEAD has moved.
    """

    RETRY_INTERVAL_SECONDS: float = 15.0

    def __init__(self, *, retry_interval_seconds: float | None = None) -> None:
        self._interval = (
            retry_interval_seconds
            if retry_interval_seconds is not None
            else self.RETRY_INTERVAL_SECONDS
        )
        self._sha: str | None = None
        self._next_retry_at: float | None = None

    @property
    def in_progress_sha(self) -> str | None:
        return self._sha

    def matches(self, sha: str) -> bool:
        """True when ``sha`` is the one we're currently polling."""
        return self._sha is not None and self._sha == sha

    def mark(self, sha: str, *, now: float) -> None:
        """Record that ``sha`` came back as IN_PROGRESS at ``now``.

        Schedules the next retry :attr:`RETRY_INTERVAL_SECONDS`
        into the future.
        """
        self._sha = sha
        self._next_retry_at = now + self._interval

    def clear(self) -> None:
        """Drop the retry state — used when HEAD moves or the server
        returns any non-IN_PROGRESS status."""
        self._sha = None
        self._next_retry_at = None

    def should_retry(self, sha: str, *, now: float) -> bool:
        """True iff ``sha`` is the in-progress sha AND the interval elapsed."""
        return self._sha == sha and self._next_retry_at is not None and now >= self._next_retry_at


__all__ = ["InProgressRetryLedger"]
