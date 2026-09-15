""" "Last sync" has to mean a sync, and a successful one.

The CodeIndex panel showed **Last sync: just now** on a repository
with nothing indexed at all — two tiles away from a coverage reading
of ``0 / 1,377 files``. Every other number on that page was true, so
the one that was not made the whole page read as placeholder data.

The cause: the activity ring buffer holds *attempts*.
``CodeIndexSyncManager._record_activity`` drops only the "watcher
tick, nothing changed" no-ops, so a failure, a skip and a sync still
running all land in it like any other row — and ``_derive_last_sync``
took the newest row without looking at ``succeeded``.

The rule these pin: no successful sync means no last sync. The empty
string is the honest answer, and the panel already renders it as an
em dash.
"""

from __future__ import annotations

import pytest

from ember_code.backend.server_codeindex import CodeIndexController
from ember_code.core.code_index.sync.schemas import ActivityEntry


class _Sync:
    """Only the accessor ``_derive_last_sync`` uses."""

    def __init__(self, entries: list[ActivityEntry]) -> None:
        self._entries = entries

    def recent_activity(self) -> list[ActivityEntry]:
        return self._entries


class _Progress:
    """Only the attribute the fallback path reads."""

    last_sync_result = None


def derive(entries: list[ActivityEntry]):
    return CodeIndexController._derive_last_sync(_Sync(entries), _Progress())


def entry(**kw) -> ActivityEntry:
    return ActivityEntry(**kw)


class TestOnlyASuccessCounts:
    def test_a_successful_sync_is_reported(self):
        ts, stats = derive(
            [entry(ts="2026-09-12T10:00:00+00:00", succeeded=True, items_upserted=7)]
        )
        assert ts == "2026-09-12T10:00:00+00:00"
        assert stats.items_upserted == 7

    def test_a_failure_is_not_a_sync(self):
        # The case that produced "just now" against an empty index.
        ts, _ = derive([entry(ts="2026-09-12T10:00:00+00:00", error="boom")])
        assert ts == ""

    def test_a_skip_is_not_a_sync(self):
        ts, _ = derive([entry(ts="2026-09-12T10:00:00+00:00", skipped=True, reason="no remote")])
        assert ts == ""

    def test_one_still_running_is_not_a_sync(self):
        ts, _ = derive([entry(ts="2026-09-12T10:00:00+00:00", in_progress=True)])
        assert ts == ""

    def test_nothing_at_all_is_not_a_sync(self):
        ts, stats = derive([])
        assert ts == ""
        assert stats.items_upserted == 0


class TestReachingPastTheFailures:
    def test_a_failure_does_not_hide_the_sync_before_it(self):
        """The common shape: it worked, then something broke.

        Reporting no last sync because the most recent attempt failed
        would lose a true fact the user wants — the index is from
        11:00, and the 12:00 attempt failed.
        """
        ts, stats = derive(
            [
                entry(ts="2026-09-12T12:00:00+00:00", error="network"),
                entry(ts="2026-09-12T11:00:00+00:00", succeeded=True, items_upserted=3),
            ]
        )
        assert ts == "2026-09-12T11:00:00+00:00"
        assert stats.items_upserted == 3

    def test_the_newest_success_wins(self):
        ts, _ = derive(
            [
                entry(ts="2026-09-12T12:00:00+00:00", succeeded=True),
                entry(ts="2026-09-12T11:00:00+00:00", succeeded=True),
            ]
        )
        assert ts == "2026-09-12T12:00:00+00:00"


@pytest.mark.parametrize("flag", ["skipped", "in_progress"])
def test_success_wins_over_its_own_other_flags(flag):
    """``succeeded`` is the field that decides.

    A row can carry more than one flag; the panel's question is only
    ever "did this one land".
    """
    ts, _ = derive([entry(ts="2026-09-12T10:00:00+00:00", succeeded=True, **{flag: True})])
    assert ts == "2026-09-12T10:00:00+00:00"
