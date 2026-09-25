"""Reconciliation — the diff behind a proposed fix.

The diff exists only because the derivation edge pinned a version
number. Pin the artifact instead and the left-hand side of this diff
is gone forever, which is the whole reason the old knowledge store
could not answer "what changed under this document".
"""

from __future__ import annotations

from ember_code.core.artifacts.models import StaleDependent
from ember_code.core.artifacts.reconcile import Reconciler


def stale(**kw) -> StaleDependent:
    base = {
        "dependent_artifact_id": "dep",
        "dependent_name": "Design",
        "dependent_n": 2,
        "source_artifact_id": "src",
        "source_name": "Schema",
        "pinned_n": 1,
        "source_head_n": 3,
    }
    return StaleDependent(**{**base, **kw})


class TestTheDiff:
    def test_it_shows_what_changed_between_the_pin_and_head(self):
        diff = Reconciler().diff("one\ntwo\nthree", "one\nTWO\nthree")
        assert "-two" in diff
        assert "+TWO" in diff

    def test_identical_content_produces_no_diff(self):
        assert Reconciler().diff("same", "same") == ""

    def test_the_labels_name_the_versions(self):
        diff = Reconciler().diff("a", "b", from_label="Schema@1", to_label="Schema@3")
        assert "Schema@1" in diff and "Schema@3" in diff

    def test_a_long_diff_is_truncated_with_a_marker(self):
        """Silent truncation reads as "that's the whole change" and
        produces a patch that misses everything past the cut."""
        before = "\n".join(f"line {i}" for i in range(500))
        after = "\n".join(f"changed {i}" for i in range(500))
        diff = Reconciler(max_diff_lines=20).diff(before, after)

        assert len(diff.split("\n")) == 21
        assert "diff truncated" in diff
        assert "more line(s) not shown" in diff

    def test_a_diff_at_the_limit_is_not_marked_truncated(self):
        diff = Reconciler(max_diff_lines=1000).diff("a", "b")
        assert "truncated" not in diff


class TestThePlan:
    def test_it_carries_the_evidence_next_to_the_document(self):
        plan = Reconciler().plan(
            stale=stale(),
            dependent_content="the dependent body",
            pinned_content="old source",
            head_content="new source",
        )
        assert plan.dependent_content == "the dependent body"
        assert "-old source" in plan.source_diff
        assert "+new source" in plan.source_diff

    def test_the_diff_labels_use_the_source_name_and_versions(self):
        plan = Reconciler().plan(
            stale=stale(),
            dependent_content="body",
            pinned_content="a",
            head_content="b",
        )
        assert "Schema@1" in plan.source_diff
        assert "Schema@3" in plan.source_diff

    def test_a_plan_without_a_proposal_says_so(self):
        plan = Reconciler().plan(
            stale=stale(),
            dependent_content="body",
            pinned_content="a",
            head_content="b",
        )
        assert plan.has_proposal is False

    def test_the_plan_carries_both_version_numbers(self):
        plan = Reconciler().plan(
            stale=stale(),
            dependent_content="body",
            pinned_content="a",
            head_content="b",
        )
        assert (plan.pinned_n, plan.source_head_n) == (1, 3)


class TestStaleSummaries:
    def test_versions_behind_is_the_gap(self):
        assert stale(pinned_n=1, source_head_n=4).versions_behind == 3

    def test_it_never_goes_negative(self):
        """A pin ahead of head means a re-pin raced a read; reporting
        "-2 versions behind" would be noise, not information."""
        assert stale(pinned_n=5, source_head_n=3).versions_behind == 0

    def test_the_summary_names_both_sides(self):
        text = stale().summary
        assert "Design" in text and "Schema" in text
        assert "@1" in text and "@3" in text
