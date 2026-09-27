"""Drift, told the way branches work.

The version chain of one document is not a dependency: revising it
produces v6 after v5 the way a commit follows a commit on the branch
you are on. What matters is the other branch — the document this one
was built on, and whether it has moved since we branched.

So the three numbers are git's: the merge base (``pinned_n``), the
upstream head (``source_head_n``), and how far they have diverged.
"""

from __future__ import annotations

import pytest

from ember_code.core.artifacts.drift import (
    MAX_CHANGES,
    Divergence,
    DriftReview,
    DriftUpdate,
)


def divergence(**kw) -> Divergence:
    base = {
        "dependent_id": "a1",
        "dependent_name": "Login flow",
        "dependent_n": 4,
        "source_id": "b2",
        "source_name": "Auth model",
        "pinned_n": 1,
        "source_head_n": 3,
    }
    return Divergence(**{**base, **kw})


class TestWhereTheBranchesStand:
    def test_it_counts_how_far_upstream_has_moved(self):
        assert divergence(pinned_n=1, source_head_n=3).versions_behind == 2

    def test_pinned_to_the_current_version_has_not_diverged(self):
        assert divergence(pinned_n=3, source_head_n=3).has_diverged is False

    def test_a_pin_ahead_of_the_head_is_not_negative_drift(self):
        # Can only happen mid-write or after a botched repair. Zero is
        # the honest answer; a negative count would render as "-1
        # versions ahead" and mean nothing.
        assert divergence(pinned_n=5, source_head_n=3).versions_behind == 0

    def test_the_summary_names_both_sides_and_the_gap(self):
        text = DriftReview.summary(divergence())
        assert "Login flow" in text
        assert "Auth model v1" in text
        assert "now at v3" in text
        assert "2 versions ahead" in text

    def test_one_version_is_not_pluralised(self):
        assert "1 version ahead" in DriftReview.summary(divergence(source_head_n=2))


class TestNothingToSay:
    def test_a_document_in_step_has_nothing_to_bring_up_to_date(self):
        with pytest.raises(ValueError, match="Nothing has diverged"):
            DriftUpdate.prompt_for(divergence(pinned_n=3, source_head_n=3))


class TestWhatTheUpdaterIsTold:
    """The update asks for the document, not for a description of one.

    The only acceptable output is the text that goes into the store,
    so anything the model wraps around it — "Here is the revised
    version:", a code fence — becomes part of the document.
    """

    def test_it_asks_for_the_document_and_nothing_else(self):
        text = DriftUpdate.prompt_for(divergence())
        assert "complete updated document" in text
        assert "No preamble" in text
        assert "written to the store verbatim" in text

    def test_it_carries_what_landed_and_both_texts(self):
        text = DriftUpdate.prompt_for(
            divergence(
                changes=[("changed", "15 minutes", "10 minutes")],
                source_head_content="Auth is a signed JWT.",
                dependent_content="The flow stores the JWT.",
            )
        )
        assert "~ 15 minutes" in text
        assert "→ 10 minutes" in text
        assert "Auth is a signed JWT." in text
        assert "The flow stores the JWT." in text

    def test_it_asks_for_a_minimal_edit(self):
        # Rewriting the parts that were already correct is not an
        # update, and the diff will show every line touched.
        text = DriftUpdate.prompt_for(divergence())
        assert "Change only what the upstream change makes wrong" in text
        assert "Keep its structure, headings and voice." in text

    def test_a_long_upstream_rewrite_is_capped_and_says_so(self):
        # A source rewritten end to end is not usefully summarised by
        # its first twenty edits, and stopping silently would let the
        # model believe it had seen all of them.
        text = DriftUpdate.prompt_for(
            divergence(changes=[("added", "", f"line {i}") for i in range(MAX_CHANGES + 5)])
        )
        assert "further changes not shown" in text

    def test_returning_the_document_unchanged_is_allowed(self):
        # Otherwise the model invents an edit to make the request feel
        # answered — which is the failure the whole ordering of these
        # prompts exists to avoid.
        text = DriftUpdate.prompt_for(divergence())
        assert "return the document exactly as it stands" in text


class TestQuoting:
    def test_a_long_block_is_elided_in_the_middle(self):
        text = DriftUpdate.prompt_for(divergence(changes=[("added", "", "x" * 900)]))
        assert "[…]" in text

    def test_whitespace_is_collapsed_so_one_change_is_one_line(self):
        text = DriftUpdate.prompt_for(divergence(changes=[("added", "", "a\n\n   b\tc")]))
        assert "+ a b c" in text
