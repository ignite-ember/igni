"""In-progress retry ledger for the HEAD watcher.

Extracted from two ``_``-prefixed fields (``_in_progress_sha``,
``_next_retry_at``) plus a module-level constant
(``IN_PROGRESS_RETRY_SECONDS``) on the old
:class:`CodeIndexSyncManager`. Now a dedicated class shared by
the manager (for reading the ``in_progress_sha`` in
:class:`SyncProgressSnapshot`) and the watcher (for the
retrigger decision).

IN_PROGRESS retries are flat (no exponential backoff) — indexing
rarely finishes faster than the cadence and a steady interval is
easier to reason about and surface in the panel.

*Failed* attempts are tracked separately and back off exponentially.
Kept apart from the in-progress sha on purpose: the panel renders
"indexing…" from :attr:`in_progress_sha`, and a sha the server has
simply never heard of is not indexing.
"""

from __future__ import annotations


class InProgressRetryLedger:
    """Tracks the one ``in_progress`` sha we're currently polling.

    Scoped to a single sha at a time — cleared whenever the
    server returns any non-in-progress status, or the watcher
    detects HEAD has moved.
    """

    RETRY_INTERVAL_SECONDS: float = 15.0

    # An attempt that neither succeeded nor came back IN_PROGRESS gets a
    # doubling backoff. Without one the watcher re-fired at its own 1 Hz poll
    # cadence: nothing about the next tick differed, so the most ordinary state
    # in the product — you committed locally and the server has not indexed
    # that sha yet (``changeset_not_found``) — made every open session a 1 Hz
    # poller against the igni API, including the preflight round trip, for as
    # long as the session stayed open.
    FAILURE_BACKOFF_START_SECONDS: float = 15.0
    FAILURE_BACKOFF_MAX_SECONDS: float = 300.0

    def __init__(
        self,
        *,
        retry_interval_seconds: float | None = None,
        failure_backoff_start_seconds: float | None = None,
        failure_backoff_max_seconds: float | None = None,
    ) -> None:
        self._interval = (
            retry_interval_seconds
            if retry_interval_seconds is not None
            else self.RETRY_INTERVAL_SECONDS
        )
        self._backoff_start = (
            failure_backoff_start_seconds
            if failure_backoff_start_seconds is not None
            else self.FAILURE_BACKOFF_START_SECONDS
        )
        self._backoff_max = (
            failure_backoff_max_seconds
            if failure_backoff_max_seconds is not None
            else self.FAILURE_BACKOFF_MAX_SECONDS
        )
        self._sha: str | None = None
        self._next_retry_at: float | None = None
        self._failed_sha: str | None = None
        self._failed_next_at: float | None = None
        self._failed_delay: float | None = None

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
        """Drop every retry / backoff record — used when a sha finally
        lands, or when the caller wants a clean slate."""
        self._sha = None
        self._next_retry_at = None
        self._failed_sha = None
        self._failed_next_at = None
        self._failed_delay = None

    def forget_unless(self, sha: str) -> None:
        """Drop retry / backoff state that is about some *other* sha.

        Called on every poll tick: HEAD moving is the one event that makes
        a recorded wait irrelevant, and it must not clear the wait for the
        sha we are actually still waiting on.
        """
        if self._sha is not None and self._sha != sha:
            self._sha = None
            self._next_retry_at = None
        if self._failed_sha is not None and self._failed_sha != sha:
            self._failed_sha = None
            self._failed_next_at = None
            self._failed_delay = None

    def should_retry(self, sha: str, *, now: float) -> bool:
        """True iff ``sha`` is the in-progress sha AND the interval elapsed."""
        return self._sha == sha and self._next_retry_at is not None and now >= self._next_retry_at

    def mark_failed(self, sha: str, *, now: float) -> None:
        """Record an attempt on ``sha`` that neither landed nor started.

        Doubles the wait each consecutive time the same sha fails, capped at
        :attr:`FAILURE_BACKOFF_MAX_SECONDS`. A different sha starts over.
        Also drops any in-progress record for ``sha`` — the server just told
        us it is not indexing.
        """
        if self._sha == sha:
            self._sha = None
            self._next_retry_at = None
        if self._failed_sha == sha and self._failed_delay is not None:
            delay = min(self._failed_delay * 2, self._backoff_max)
        else:
            delay = self._backoff_start
        self._failed_sha = sha
        self._failed_delay = delay
        self._failed_next_at = now + delay

    def is_backing_off(self, sha: str, *, now: float) -> bool:
        """True while ``sha``'s failure backoff has not yet elapsed."""
        return (
            self._failed_sha == sha
            and self._failed_next_at is not None
            and now < self._failed_next_at
        )


__all__ = ["InProgressRetryLedger"]
