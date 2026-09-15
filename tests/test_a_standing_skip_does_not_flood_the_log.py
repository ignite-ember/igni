"""A standing reason should not evict the history that explains it.

The HEAD watcher polls once a second. On a client that cannot sync at
all — no server configured, repository not connected — every one of
those polls recorded a skip carrying the same sentence, so the
twenty-slot ring buffer refilled itself every twenty seconds. The
panel's activity list could only ever show the last twenty seconds of
one repeated complaint, and any real sync that happened before it had
already been pushed out.

Consecutive skips sharing a reason now collapse onto one row, which
keeps its timestamp current. Errors and successes never collapse:
twenty failures in a row are twenty separate facts, and the spacing
between them is information.
"""

from __future__ import annotations

from ember_code.core.code_index.sync.activity_log import SyncActivityLog
from ember_code.core.code_index.sync.schemas import ActivityEntry


def skip(reason: str, ts: str) -> ActivityEntry:
    return ActivityEntry(ts=ts, skipped=True, reason=reason)


def ok(ts: str, upserted: int = 1) -> ActivityEntry:
    return ActivityEntry(ts=ts, succeeded=True, items_upserted=upserted)


def fail(error: str, ts: str) -> ActivityEntry:
    return ActivityEntry(ts=ts, error=error)


class TestCollapsingAStandingSkip:
    def test_a_repeated_skip_occupies_one_row(self):
        log = SyncActivityLog()
        for i in range(50):
            log.record(skip("no igni server configured", f"2026-09-12T12:00:{i:02d}+00:00"))
        assert len(log.recent()) == 1

    def test_the_row_keeps_the_latest_timestamp(self):
        # It is still happening; the row should say so.
        log = SyncActivityLog()
        log.record(skip("no server", "2026-09-12T12:00:00+00:00"))
        log.record(skip("no server", "2026-09-12T12:05:00+00:00"))
        assert log.recent()[0].ts == "2026-09-12T12:05:00+00:00"

    def test_a_different_reason_starts_a_new_row(self):
        log = SyncActivityLog()
        log.record(skip("no server", "2026-09-12T12:00:00+00:00"))
        log.record(skip("repository not connected", "2026-09-12T12:00:01+00:00"))
        assert [e.reason for e in log.recent()] == ["repository not connected", "no server"]

    def test_the_real_history_survives_the_flood(self):
        """The point of the change.

        A successful sync, then half an hour of a standing skip. The
        sync has to still be there — it is the only evidence the index
        was ever built.
        """
        log = SyncActivityLog()
        log.record(ok("2026-09-12T11:00:00+00:00", upserted=42))
        for i in range(1800):
            log.record(skip("no server", f"2026-09-12T12:{i // 60:02d}:{i % 60:02d}+00:00"))
        assert any(e.succeeded and e.items_upserted == 42 for e in log.recent())


class TestWhatMustNotCollapse:
    def test_repeated_failures_each_get_a_row(self):
        log = SyncActivityLog()
        for i in range(5):
            log.record(fail("connection refused", f"2026-09-12T12:00:{i:02d}+00:00"))
        assert len(log.recent()) == 5

    def test_repeated_successes_each_get_a_row(self):
        log = SyncActivityLog()
        for i in range(5):
            log.record(ok(f"2026-09-12T12:00:{i:02d}+00:00"))
        assert len(log.recent()) == 5

    def test_the_window_still_bounds_everything_else(self):
        log = SyncActivityLog()
        for i in range(100):
            log.record(ok(f"2026-09-12T12:00:{i:02d}+00:00"))
        assert len(log.recent()) == SyncActivityLog.DEFAULT_LIMIT
