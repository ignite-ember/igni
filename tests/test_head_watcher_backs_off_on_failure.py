"""A sync that neither landed nor started indexing must not re-fire at 1 Hz.

``HeadWatcher`` polls git HEAD once a second and syncs when the sha is not the
last synced one. Nothing updates ``last_synced_sha`` on a failed attempt and the
retry ledger was cleared on every outcome except ``IN_PROGRESS``, so the
predicate stayed true forever: the most ordinary state in the product — you
committed locally and the server has not indexed that sha (``changeset_not_found``)
— turned every open session into a 1 Hz poller against the igni API, preflight
round trip included, with no backoff.

These tests pin the count. Before the fix the failure cases below produced one
sync per tick (tens of them in the window these tests run for) instead of one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ember_code.core.code_index.sync.head_watcher import HeadWatcher
from ember_code.core.code_index.sync.retry_ledger import InProgressRetryLedger

TICK = 0.01
WINDOW = 0.25  # ~25 ticks


@dataclass
class _Result:
    """Enough of ``SyncResult`` for the watcher's two branches."""

    in_progress: bool = False


class _Harness:
    """A repo whose HEAD never moves and whose syncs never land."""

    def __init__(self, *, ledger: InProgressRetryLedger, in_progress: bool = False, head="deadbee"):
        self.attempts: list[str] = []
        self.head = head
        self._in_progress = in_progress
        self.last_synced: str | None = None
        self.watcher = HeadWatcher(
            get_head=lambda: self.head,
            run_sync=self._run_sync,
            retry_ledger=ledger,
            last_synced_sha_getter=lambda: self.last_synced,
            interval_seconds=TICK,
        )

    async def _run_sync(self, sha: str) -> _Result:
        self.attempts.append(sha)
        return _Result(in_progress=self._in_progress)

    async def run(self, seconds: float = WINDOW) -> None:
        await self.watcher.start()
        await asyncio.sleep(seconds)
        await self.watcher.stop()


class TestAFailedSyncBacksOff:
    async def test_an_unindexed_sha_is_attempted_once_not_once_per_tick(self):
        """``changeset_not_found``: reached the server, nothing to pull."""
        harness = _Harness(ledger=InProgressRetryLedger())

        await harness.run()

        assert harness.attempts == ["deadbee"], (
            f"{len(harness.attempts)} syncs in {WINDOW}s — the watcher is re-running "
            "the full sync, server preflight included, at its poll cadence"
        )

    async def test_the_backoff_does_expire(self):
        """Backing off is not giving up — the server does eventually index it."""
        ledger = InProgressRetryLedger(
            failure_backoff_start_seconds=0.03,
            failure_backoff_max_seconds=0.03,
        )
        harness = _Harness(ledger=ledger)

        await harness.run(seconds=0.2)

        assert 2 <= len(harness.attempts) <= 8, (
            f"{len(harness.attempts)} attempts — expected a handful of retries at the "
            "30ms backoff, not one per 10ms tick and not a permanent stop"
        )

    async def test_consecutive_failures_double_the_wait(self):
        ledger = InProgressRetryLedger(
            failure_backoff_start_seconds=10.0,
            failure_backoff_max_seconds=100.0,
        )

        ledger.mark_failed("sha", now=0.0)
        assert ledger.is_backing_off("sha", now=9.9)
        assert not ledger.is_backing_off("sha", now=10.1)

        ledger.mark_failed("sha", now=10.1)
        assert ledger.is_backing_off("sha", now=29.9), "second failure did not double the wait"
        assert not ledger.is_backing_off("sha", now=30.2)

    def test_the_backoff_is_capped(self):
        ledger = InProgressRetryLedger(
            failure_backoff_start_seconds=60.0,
            failure_backoff_max_seconds=90.0,
        )
        for _ in range(10):
            ledger.mark_failed("sha", now=0.0)
        assert not ledger.is_backing_off("sha", now=90.1)


class TestTheBackoffIsScopedToTheSha:
    async def test_a_new_commit_syncs_immediately(self):
        """Backing off on a sha must not delay the next commit you make."""
        harness = _Harness(ledger=InProgressRetryLedger())

        await harness.watcher.start()
        await asyncio.sleep(0.05)
        harness.head = "cafe123"
        await asyncio.sleep(0.05)
        await harness.watcher.stop()

        assert harness.attempts == ["deadbee", "cafe123"], harness.attempts


class TestInProgressKeepsItsOwnCadence:
    async def test_in_progress_still_uses_the_flat_retry_interval(self):
        """The IN_PROGRESS path is unchanged: a flat 15s poll, not a doubling one."""
        ledger = InProgressRetryLedger(retry_interval_seconds=0.03)
        harness = _Harness(ledger=ledger, in_progress=True)

        await harness.run(seconds=0.2)

        assert 2 <= len(harness.attempts) <= 8, harness.attempts
        assert ledger.in_progress_sha == "deadbee"

    async def test_a_failure_does_not_leave_the_panel_saying_indexing(self):
        """``in_progress_sha`` drives the panel's "indexing…" state (see
        ``server_codeindex.py``); a sha the server has never heard of is not
        indexing, so the failure backoff is tracked separately."""
        ledger = InProgressRetryLedger()
        harness = _Harness(ledger=ledger)

        await harness.run(seconds=0.05)

        assert ledger.in_progress_sha is None
        assert ledger.is_backing_off("deadbee", now=0.0)


class TestASuccessfulSyncClearsEverything:
    async def test_a_landed_sha_leaves_no_backoff_behind(self):
        harness = _Harness(ledger=InProgressRetryLedger())

        async def landing_sync(sha: str) -> _Result:
            harness.attempts.append(sha)
            harness.last_synced = sha
            return _Result(in_progress=False)

        harness.watcher._run_sync = landing_sync  # noqa: SLF001 — swapping the injected dep
        await harness.run(seconds=0.05)

        assert harness.attempts == ["deadbee"]
        assert not harness.watcher._retry_ledger.is_backing_off(  # noqa: SLF001
            "deadbee", now=0.0
        ), "a sync that landed must not leave a backoff that delays the next commit"
