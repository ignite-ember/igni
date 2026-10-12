"""Reading the agent's verdict on a captured read.

The review row now leads with the agent's recommendation instead of a
similarity score, which moves the risk: a reviewer who trusts a
recommendation is trusting the parse of it. Every test here is about
the parse refusing to invent an answer — an unreadable reply has to
land on ``unclear``, because a guessed recommendation is exactly the
false confidence the verdict was added to remove.
"""

from __future__ import annotations

import pytest

from ember_code.core.artifacts.sourcing import (
    MAX_REASON_CHARS,
    CandidateReview,
    Overlap,
    Recommendation,
)


def review(**over) -> CandidateReview:
    base = dict(
        dependent_name="Client retry guide",
        dependent_content="# Client retry guide\n\nHonour Retry-After exactly.",
        source_name="Rate limiting",
        source_n=3,
        source_content="# Rate limiting\n\nRetry-After is seconds.",
        overlaps=[],
        passages_touched=0,
        total_passages=5,
    )
    base.update(over)
    return CandidateReview(**base)


OVERLAP = Overlap(
    here_text="honour `Retry-After` exactly",
    here_line_from=12,
    here_line_to=14,
    there_text="`Retry-After` is seconds remaining",
    there_line_from=30,
    there_line_to=32,
    similarity=0.81,
)


class TestReadingTheAnswer:
    def test_a_plain_verdict(self):
        v = CandidateReview.parse(
            "VERDICT: source\n\nThe backing-off section restates the Retry-After rule."
        )
        assert v.recommendation is Recommendation.SOURCE
        assert "backing-off section" in v.reason
        # The machine-readable line is not part of the prose.
        assert "VERDICT" not in v.reason

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("VERDICT: not-a-source", Recommendation.NOT_A_SOURCE),
            ("VERDICT: not a source", Recommendation.NOT_A_SOURCE),
            ("VERDICT: **source**", Recommendation.SOURCE),
            ("verdict: Source", Recommendation.SOURCE),
            ("VERDICT: `unclear`", Recommendation.UNCLEAR),
            ("VERDICT: uncertain", Recommendation.UNCLEAR),
        ],
    )
    def test_the_spellings_a_model_actually_writes(self, line, expected):
        assert CandidateReview.parse(f"{line}\n\nBecause.").recommendation is expected

    def test_a_verdict_further_down_the_reply(self):
        v = CandidateReview.parse("Let me think about this.\n\nVERDICT: source\n\nBecause.")
        assert v.recommendation is Recommendation.SOURCE


class TestItRefusesToInventOne:
    def test_prose_with_no_verdict_line_is_unclear(self):
        # The important one. "This is clearly a source" reads as a
        # recommendation and is not one — nothing was asked to commit.
        v = CandidateReview.parse("This is clearly a source, the two documents match.")
        assert v.recommendation is Recommendation.UNCLEAR
        assert v.reason  # the text survives, so the reviewer can read it

    def test_the_template_echoed_back_is_not_a_choice(self):
        v = CandidateReview.parse("VERDICT: source | not-a-source | unclear\n\nHmm.")
        assert v.recommendation is Recommendation.UNCLEAR

    def test_an_unrecognised_word_is_unclear(self):
        assert (
            CandidateReview.parse("VERDICT: probably\n\nBecause.").recommendation
            is Recommendation.UNCLEAR
        )

    def test_an_empty_reply(self):
        v = CandidateReview.parse("")
        assert v.recommendation is Recommendation.UNCLEAR
        assert v.reason == ""

    def test_a_long_reply_is_cut_rather_than_shown_whole(self):
        v = CandidateReview.parse("VERDICT: source\n\n" + "word " * 400)
        assert len(v.reason) <= MAX_REASON_CHARS + 1
        assert v.reason.endswith("…")


class TestWhatTheAgentIsAsked:
    def test_both_documents_are_in_the_prompt(self):
        # A one-shot call with no tools reads only what it is handed.
        prompt = review().prompt_for()
        assert "Honour Retry-After exactly." in prompt
        assert "Retry-After is seconds." in prompt

    def test_it_says_capture_is_not_evidence_of_use(self):
        # The distinction the whole review exists for.
        prompt = review().prompt_for()
        assert "read" in prompt and "not that it was used" in prompt

    def test_measured_overlaps_are_quoted_with_their_scores(self):
        prompt = review(overlaps=[OVERLAP], passages_touched=1).prompt_for()
        assert "0.81" in prompt
        assert "L12–14" in prompt
        assert "1 of 5" in prompt

    def test_no_overlap_is_stated_rather_than_left_out(self):
        # Silence would read as "no measurement was taken".
        prompt = review().prompt_for()
        assert "No passage here resembles anything there" in prompt

    def test_it_warns_that_similarity_is_not_derivation(self):
        prompt = review(overlaps=[OVERLAP]).prompt_for()
        assert "not a verdict" in prompt

    def test_unclear_is_offered_as_a_real_answer(self):
        # Without this the model picks one of two and the reviewer
        # cannot tell a judgement from a coin toss.
        prompt = review().prompt_for()
        assert "unclear" in prompt
        assert "Prefer this to a confident guess" in prompt

    def test_it_names_the_cost_of_a_false_confirm(self):
        assert "ignore alarms" in review().prompt_for()

    def test_a_long_document_is_clipped_not_dropped(self):
        prompt = review(dependent_content="x" * 40_000).prompt_for()
        assert "Client retry guide" in prompt
        assert len(prompt) < 30_000
