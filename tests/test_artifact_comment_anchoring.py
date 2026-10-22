"""What became of the passage a comment was left on.

The review queue said "the document has moved on since" against every
row. True of all of them, and it separates none of them: a passage
still present word for word and a passage deleted outright get the
same sentence, and those are opposite decisions.

The distinction these tests hold is the one the whole design rests
on — **a changed anchor is not an answered comment.** Measured on the
live store: a comment asking to "add a sentence saying where the
denylist lives" had its passage edited to 0.88 similarity, because the
edit added the cookie's name and lifetime. The anchor moved; the
comment was untouched by it. Anything that concluded "changed,
therefore stale" would have closed a live comment.
"""

from __future__ import annotations

import pytest

from ember_code.core.artifacts.anchoring import (
    COSMETIC,
    SAME_PASSAGE,
    AnchorState,
    Relevance,
    RelevanceReview,
    surrounding,
    track_anchor,
)

QUOTE = "Auth is a signed JWT, not a session cookie. It expires in 15 minutes."

INTACT = f"# Auth model\n\n{QUOTE}\n\nRevocation is checked on refresh only."

EDITED = (
    "# Auth model\n\n"
    "Auth is a signed JWT, not a session cookie. It expires in 15 minutes and is "
    "refreshed by an httpOnly cookie named `igni_rt`.\n\n"
    "Revocation is checked on refresh only."
)

REWRITTEN = (
    "# Auth model\n\nSessions are opaque tokens held server-side.\n\n"
    "The store is Redis, with a sliding window."
)


class TestWhatTheTextDid:
    def test_a_passage_still_there_word_for_word(self):
        fate = track_anchor(QUOTE, INTACT)
        assert fate.state is AnchorState.INTACT
        assert fate.similarity == 1.0
        assert fate.survived

    def test_a_passage_edited_is_found_and_scored(self):
        fate = track_anchor(QUOTE, EDITED)
        assert fate.state is AnchorState.EDITED
        assert fate.survived
        # And it carries what the passage says *now*, so the reviewer
        # reads the change rather than being told there was one.
        assert "igni_rt" in fate.now
        assert fate.similarity >= SAME_PASSAGE

    def test_a_passage_rewritten_past_recognition_is_gone(self):
        fate = track_anchor(QUOTE, REWRITTEN)
        assert fate.state is AnchorState.GONE
        assert not fate.survived
        # No "now": claiming the nearest paragraph replaced it would
        # point the reviewer at text that has nothing to do with it.
        assert fate.now == ""

    def test_whitespace_and_wrapping_are_not_a_change(self):
        # Prose is hard-wrapped; where the lines break is not an edit.
        rewrapped = INTACT.replace("It expires in 15 minutes.", "It expires\nin 15 minutes.")
        assert track_anchor(QUOTE, rewrapped).state is AnchorState.INTACT

    def test_a_comma_is_not_a_change(self):
        # Above COSMETIC the reviewer is told it survived, because for
        # judging the comment it did — sending them to read a diff
        # that is one character wastes the trip.
        nearly = INTACT.replace("15 minutes.", "15 minutes")
        fate = track_anchor(QUOTE, nearly)
        assert fate.state is AnchorState.INTACT
        assert fate.similarity >= COSMETIC

    def test_a_comment_with_no_quote(self):
        # About the document at large. Not a failure, and not "gone".
        fate = track_anchor("", INTACT)
        assert fate.state is AnchorState.NO_ANCHOR
        assert not fate.survived

    def test_an_empty_document(self):
        assert track_anchor(QUOTE, "").state is AnchorState.GONE

    def test_the_nearest_block_wins_not_the_whole_document(self):
        # Compared block by block: a ratio against ten thousand
        # characters is dominated by everything the quote is not about.
        padded = EDITED + "\n\n" + ("Unrelated prose about deployments. " * 200)
        fate = track_anchor(QUOTE, padded)
        assert fate.state is AnchorState.EDITED
        assert "igni_rt" in fate.now


class TestTheContextItCarries:
    def test_it_includes_the_neighbouring_paragraphs(self):
        # A passage alone answers "is it still here" and not "has the
        # thing asked for been done" — the fix often lands in the
        # sentence after.
        context = surrounding(INTACT, QUOTE)
        assert "Revocation is checked" in context

    def test_it_is_bounded(self):
        long_doc = "\n\n".join(f"Paragraph {i}. " + "x" * 400 for i in range(20))
        assert len(surrounding(long_doc, "Paragraph 5.")) <= 400

    def test_an_empty_document_yields_nothing(self):
        assert surrounding("", QUOTE) == ""


class TestReadingTheAgentsAnswer:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("VERDICT: still-applies", Relevance.STILL_APPLIES),
            ("VERDICT: still applies", Relevance.STILL_APPLIES),
            ("VERDICT: overtaken", Relevance.OVERTAKEN),
            ("VERDICT: **overtaken**", Relevance.OVERTAKEN),
            ("verdict: Unclear", Relevance.UNCLEAR),
        ],
    )
    def test_the_spellings_a_model_writes(self, line, expected):
        assert RelevanceReview.parse(f"{line}\n\nBecause.").relevance is expected

    def test_prose_with_no_verdict_line_is_unclear(self):
        # "This is clearly overtaken" reads like a recommendation and
        # is not one — nothing was asked to commit.
        verdict = RelevanceReview.parse("This is clearly overtaken by the rewrite.")
        assert verdict.relevance is Relevance.UNCLEAR
        assert verdict.reason  # kept, so the reviewer can read it

    def test_the_template_echoed_back_is_not_a_choice(self):
        parsed = RelevanceReview.parse("VERDICT: still-applies | overtaken | unclear\n\nHm.")
        assert parsed.relevance is Relevance.UNCLEAR

    def test_the_verdict_line_is_not_left_in_the_reason(self):
        assert "VERDICT" not in RelevanceReview.parse("VERDICT: overtaken\n\nDone in v4.").reason


class TestWhatTheAgentIsAsked:
    @staticmethod
    def review(content: str, body: str = "Add a sentence saying where the denylist lives."):
        return RelevanceReview(
            document_name="Auth model",
            comment_body=body,
            author="dana@acme.test",
            commented_n=2,
            head_n=6,
            fate=track_anchor(QUOTE, content),
            context=surrounding(content, QUOTE),
        )

    def test_an_edited_anchor_is_shown_both_ways(self):
        prompt = self.review(EDITED).prompt_for()
        assert "Then:" in prompt and "Now:" in prompt
        assert "igni_rt" in prompt

    def test_a_gone_anchor_says_why_it_is_called_gone(self):
        # With the number, so the judgement is inspectable rather than
        # asserted.
        prompt = self.review(REWRITTEN).prompt_for()
        assert "no longer in the document" in prompt
        assert "which is why it is being called gone" in prompt

    def test_it_warns_that_a_changed_anchor_is_not_an_answered_comment(self):
        # The measured failure: a passage edited to 0.88 while the
        # thing asked for went untouched.
        assert "not the same as an answered comment" in self.review(EDITED).prompt_for()

    def test_unclear_is_offered_with_its_cost(self):
        prompt = self.review(EDITED).prompt_for()
        assert "unclear" in prompt
        assert "stop leaving comments" in prompt

    def test_the_comment_itself_is_in_the_prompt(self):
        assert "denylist lives" in self.review(EDITED).prompt_for()

    def test_a_suggestion_is_carried_when_there_is_one(self):
        review = self.review(EDITED)
        review.suggested_text = "Revoked tokens live in Redis."
        assert "Revoked tokens live in Redis." in review.prompt_for()
