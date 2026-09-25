"""The retrieval ledger — what a write is offered as candidate sources.

Capture exists because an agent that reads three documents cites one.
These tests pin the two asymmetries that make it safe: capture is
generous (a read is remembered even if never cited) and bounded (a
long session does not turn the first thing read into a dependency of
the last thing written).
"""

from __future__ import annotations

from ember_code.core.artifacts.capture import RetrievalLedger
from ember_code.core.artifacts.models import EdgeState, VersionPin


class TestRecordingReads:
    def test_a_read_becomes_a_candidate(self):
        ledger = RetrievalLedger()
        ledger.record([VersionPin(artifact_id="a1", n=3)])
        candidates = ledger.candidates()
        assert [c.ref for c in candidates] == ["a1@3"]
        assert candidates[0].state is EdgeState.CANDIDATE

    def test_a_confirmed_pin_is_still_recorded_as_a_candidate(self):
        """The ledger records reading, not asserting.

        A pin arriving with CONFIRMED state came from somewhere else;
        letting it through unchanged would let a citation elsewhere in
        the turn silently promote an unrelated read.
        """
        ledger = RetrievalLedger()
        ledger.record([VersionPin(artifact_id="a1", n=1, state=EdgeState.CONFIRMED)])
        assert ledger.candidates()[0].state is EdgeState.CANDIDATE

    def test_rereading_refreshes_the_version(self):
        ledger = RetrievalLedger()
        ledger.record([VersionPin(artifact_id="a1", n=1)])
        ledger.record([VersionPin(artifact_id="a1", n=4)])
        assert [c.ref for c in ledger.candidates()] == ["a1@4"]
        assert len(ledger) == 1


class TestExclusions:
    def test_declared_sources_are_not_offered_again(self):
        ledger = RetrievalLedger()
        ledger.record([VersionPin(artifact_id="a1", n=1), VersionPin(artifact_id="a2", n=1)])
        assert [c.artifact_id for c in ledger.candidates(exclude=["a1"])] == ["a2"]

    def test_an_artifact_never_becomes_its_own_source(self):
        """A revision reads its own previous version on most turns."""
        ledger = RetrievalLedger()
        ledger.record([VersionPin(artifact_id="self", n=2)])
        assert ledger.candidates(exclude=["self"]) == []

    def test_forget_drops_a_deleted_artifact(self):
        ledger = RetrievalLedger()
        ledger.record([VersionPin(artifact_id="gone", n=1)])
        ledger.forget("gone")
        assert ledger.candidates() == []


class TestBounds:
    def test_the_oldest_read_is_evicted_past_capacity(self):
        ledger = RetrievalLedger(capacity=2)
        ledger.record([VersionPin(artifact_id="a", n=1)])
        ledger.record([VersionPin(artifact_id="b", n=1)])
        ledger.record([VersionPin(artifact_id="c", n=1)])
        assert [c.artifact_id for c in ledger.candidates()] == ["b", "c"]

    def test_capacity_is_never_zero(self):
        """A zero capacity would silently disable capture entirely."""
        ledger = RetrievalLedger(capacity=0)
        ledger.record([VersionPin(artifact_id="a", n=1)])
        assert len(ledger) == 1

    def test_clear_empties_the_ledger(self):
        ledger = RetrievalLedger()
        ledger.record([VersionPin(artifact_id="a", n=1)])
        ledger.clear()
        assert len(ledger) == 0
