"""The drift suite's scorer.

Worth testing on its own: a scorer that cannot fail is worse than no
suite at all, because it reports a pass either way. These cases feed
it outputs that are wrong in each of the three ways a drift update can
be wrong, and check it says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.evals.drift.run_live import (  # noqa: E402
    Score,
    _flat,
    divergence_for,
    grade,
)

CASE = {
    "name": "t",
    "expect": {
        "removed": ["X-RateLimit-Remaining", "burst allowance of 100"],
        "present": ["RateLimit-Remaining"],
        "preserved": ["/healthz"],
    },
}


class TestItCatchesEachKindOfMiss:
    def test_a_perfect_update_passes(self):
        score = grade(CASE, "Read RateLimit-Remaining. /healthz is exempt.")
        assert score.passed
        assert score.missed == []

    def test_a_stale_fact_that_survived_is_named(self):
        # The question the suite exists to answer. A count would say
        # "1 missed"; the name says which one got away.
        score = grade(CASE, "Read X-RateLimit-Remaining. /healthz is exempt.")
        assert not score.passed
        assert score.missed == ["X-RateLimit-Remaining"]

    def test_several_stale_facts_are_all_named(self):
        # "3 of 4" is the interesting failure, so it must not stop at
        # the first one it finds.
        score = grade(CASE, "Read X-RateLimit-Remaining with a burst allowance of 100. /healthz.")
        assert sorted(score.missed) == ["X-RateLimit-Remaining", "burst allowance of 100"]

    def test_the_replacement_never_arriving_is_a_failure(self):
        score = grade(CASE, "Something else entirely. /healthz is exempt.")
        assert "RateLimit-Remaining" in score.absent

    def test_losing_an_untouched_passage_is_a_failure(self):
        # The opposite failure: fixing the document by replacing it.
        score = grade(CASE, "Read RateLimit-Remaining.")
        assert score.lost == ["/healthz"]
        assert not score.passed

    def test_an_empty_answer_fails_rather_than_passing_vacuously(self):
        score = grade(CASE, "")
        assert not score.passed


class TestNotReachingTheModelIsNotAFailingUpdate:
    """The distinction an exhausted token plan taught this suite.

    Three empty answers were graded and reported as a tidy ``0/3``,
    which reads as a regression in the prompt when nothing had changed
    but the account balance.
    """

    def test_an_error_is_not_reached(self):
        score = Score(name="t", missed=[], absent=[], lost=[], output="", error="429")
        assert not score.reached
        assert not score.passed

    def test_an_empty_answer_is_not_reached_either(self):
        # Agno logs the provider error and hands back empty content
        # rather than raising, so an exception is not the only shape
        # this failure arrives in.
        score = Score(name="t", missed=[], absent=[], lost=[], output="   ")
        assert not score.reached

    def test_a_real_answer_is_reached_even_when_it_is_wrong(self):
        score = Score(name="t", missed=["still stale"], absent=[], lost=[], output="text")
        assert score.reached
        assert not score.passed


class TestMatching:
    def test_it_ignores_case_because_this_is_about_facts(self):
        score = grade(CASE, "read ratelimit-remaining. /HEALTHZ is exempt.")
        assert score.passed

    def test_it_matches_across_a_line_break(self):
        # Prose wraps. A phrase that sits across a break in the source
        # will wrap somewhere else in the answer, and an unflattened
        # check matches neither side — so it can never fail, which
        # reads as coverage while providing none.
        assert _flat("burst allowance\nof   100") == "burst allowance of 100"
        score = grade(
            {"name": "t", "expect": {"removed": ["burst allowance of 100"]}},
            "…with a burst\nallowance of 100.",
        )
        assert score.missed == ["burst allowance of 100"]

    def test_a_substring_of_the_replacement_does_not_excuse_the_original(self):
        # ``RateLimit-Remaining`` is inside ``X-RateLimit-Remaining``.
        # A naive "is the new name present" check passes an output
        # that still carries the old one.
        score = grade(CASE, "Read X-RateLimit-Remaining. /healthz is exempt.")
        assert score.absent == []
        assert score.missed == ["X-RateLimit-Remaining"]
        assert not score.passed


class TestTheCasesThemselves:
    """The fixtures have to be able to fail."""

    @staticmethod
    def cases() -> list[dict]:
        return yaml.safe_load((REPO_ROOT / "evals" / "drift.yaml").read_text())["cases"]

    def test_every_case_grades_the_document_it_starts_from_as_failing(self):
        # Handing back the document untouched must fail every case
        # that expects a change — otherwise the case proves nothing.
        for case in self.cases():
            if not case["expect"].get("removed"):
                continue
            score = grade(case, case["document"])
            assert not score.passed, f"{case['name']} passes without changing anything"

    def test_the_unaffected_case_passes_on_an_untouched_document(self):
        # Its whole point: the right answer is to change nothing.
        case = next(c for c in self.cases() if c["name"] == "nothing_here_is_affected")
        assert grade(case, case["document"]).passed

    def test_every_removed_string_is_actually_in_the_starting_document(self):
        # A ``removed`` entry that was never there is a check that can
        # never fail, and reads as coverage it is not providing. This
        # caught one: "burst allowance of 100" sits across a line
        # break, so an unflattened comparison found it in neither the
        # document nor any answer.
        for case in self.cases():
            for stale in case["expect"].get("removed", []):
                assert _flat(stale) in _flat(case["document"]), (
                    f"{case['name']}: {stale!r} is not in the document to begin with"
                )

    def test_every_preserved_string_is_actually_in_the_starting_document(self):
        for case in self.cases():
            for kept in case["expect"].get("preserved", []):
                assert _flat(kept) in _flat(case["document"]), (
                    f"{case['name']}: {kept!r} is not in the document to begin with"
                )

    def test_no_removed_string_appears_in_the_corrected_source(self):
        # The other half of the vacuous check, and the harder one to
        # see. If the phrase we forbid is also in the *corrected*
        # upstream, a document that quotes the correction correctly
        # fails — the check punishes the right answer.
        for case in self.cases():
            after = _flat(case["source_after"])
            for stale in case["expect"].get("removed", []):
                assert _flat(stale) not in after, (
                    f"{case['name']}: {stale!r} is in the corrected source, so a "
                    "document that repeats it is right and would still fail"
                )

    def test_every_removed_string_is_a_claim_rather_than_a_keyword(self):
        # This one cost a run. "Unix timestamp" failed a document that
        # said "seconds remaining — treat it as a duration, not a Unix
        # timestamp", which is exactly the correction we asked for: a
        # short noun phrase survives inside the sentence that refutes
        # it. Only a whole wrong claim has nowhere to hide, and three
        # words is the cheapest stand-in for "whole claim" that a test
        # can check.
        for case in self.cases():
            for stale in case["expect"].get("removed", []):
                assert len(_flat(stale).split()) >= 3, (
                    f"{case['name']}: {stale!r} is a keyword, not a claim — a correct "
                    "document can contain it while negating it. Forbid the wrong "
                    "instruction instead."
                )

    def test_each_case_produces_a_prompt_with_the_change_in_it(self):
        for case in self.cases():
            divergence = divergence_for(case)
            assert divergence.has_diverged, case["name"]
            assert divergence.changes, f"{case['name']} has no detectable upstream change"
            prompt = __import__(
                "ember_code.core.artifacts.drift", fromlist=["DriftUpdate"]
            ).DriftUpdate.prompt_for(divergence)
            assert case["document"][:40] in prompt


@pytest.mark.parametrize("field", ["source_before", "source_after", "document"])
def test_every_case_defines_the_texts_it_needs(field):
    cases = yaml.safe_load((REPO_ROOT / "evals" / "drift.yaml").read_text())["cases"]
    for case in cases:
        assert case[field].strip(), f"{case['name']} has an empty {field}"
