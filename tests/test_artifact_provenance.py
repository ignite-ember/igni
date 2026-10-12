"""Matching a document's passages onto its source's passages.

The value of this is entirely in what it *refuses* to claim. A
confident line drawn between two paragraphs that have nothing to do
with each other is worse than no line at all, so most of what is
pinned here is the floor, the tie-breaking, and the handling of
chunks that carry no vector.
"""

from __future__ import annotations

import math

from ember_code.core.artifacts.provenance import (
    CLOSE,
    FLOOR,
    VERY_CLOSE,
    Match,
    Passage,
    PassageProvenance,
    ProvenanceMapper,
    cosine,
    quote_lines,
    strength_of,
)
from ember_code.core.artifacts.side_by_side import DiffRow, RowKind


def unit(*values: float) -> list[float]:
    """A normalised vector, so similarities are easy to reason about."""
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def at_similarity(score: float) -> list[float]:
    """A unit vector whose cosine against ``unit(1, 0)`` is ``score``.

    Lets a case say "this is what noise looks like" in the numbers
    actually measured against a real store, rather than in an angle
    picked because it was easy to write down.
    """
    return [score, math.sqrt(max(0.0, 1.0 - score * score))]


#: What the real store produced, and what the floor was set against:
#: passages sharing only a topic, and a genuine derivation.
MEASURED_NOISE_CEILING: float = 0.56
MEASURED_SIGNAL_FLOOR: float = 0.74


def passage(cid: str, *, vec: list[float] | None = None, lf: int = 1, lt: int = 1, text: str = "x"):
    return Passage(chunk_id=cid, text=text, line_from=lf, line_to=lt, embedding=vec)


class TestCosine:
    def test_identical_vectors_score_one(self):
        v = unit(1.0, 2.0, 3.0)
        assert cosine(v, v) == 1.0

    def test_orthogonal_vectors_score_zero(self):
        assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_a_chunk_with_no_vector_is_not_a_perfect_match(self):
        # Chunks written before embeddings were stored have none.
        # Treating a missing vector as distance zero would make every
        # old passage match everything.
        assert cosine(None, [1.0, 0.0]) == 0.0
        assert cosine([1.0, 0.0], []) == 0.0

    def test_mismatched_dimensions_do_not_raise(self):
        assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_a_zero_vector_does_not_divide_by_zero(self):
        assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestWhatItRefuses:
    def test_a_weak_resemblance_is_dropped_not_shown_faintly(self):
        # A document and a source on the same topic score middling on
        # nearly every pairing. Showing those would mark the whole
        # document as derived from the whole source and say nothing.
        doc = [passage("d1", vec=unit(1.0, 0.0))]
        # 0.56 — the strongest same-topic pairing measured on a real
        # store, where the two passages had nothing to do with each
        # other beyond both being about authentication.
        src = [passage("s1", vec=at_similarity(MEASURED_NOISE_CEILING))]

        mapped = ProvenanceMapper.map(doc, src)

        assert mapped[0].matches == []
        assert mapped[0].best is None

    def test_a_passage_with_no_vector_matches_nothing(self):
        doc = [passage("d1", vec=None)]
        src = [passage("s1", vec=unit(1.0, 0.0))]
        assert ProvenanceMapper.map(doc, src)[0].matches == []

    def test_every_passage_is_reported_even_with_no_match(self):
        # The caller needs a row per passage to render a gutter; a
        # filtered list would silently shift every highlight.
        doc = [passage("d1", vec=unit(1.0, 0.0)), passage("d2", vec=None)]
        mapped = ProvenanceMapper.map(doc, [])
        assert [m.chunk_id for m in mapped] == ["d1", "d2"]

    def test_it_keeps_only_the_strongest_few(self):
        doc = [passage("d1", vec=unit(1.0, 0.0))]
        src = [passage(f"s{i}", vec=unit(1.0, 0.0)) for i in range(8)]
        assert len(ProvenanceMapper.map(doc, src)[0].matches) == 3


class TestWhatItMatches:
    def test_an_exact_resemblance_is_matched(self):
        v = unit(1.0, 0.0, 0.0)
        mapped = ProvenanceMapper.map([passage("d1", vec=v)], [passage("s1", vec=v)])
        assert mapped[0].best is not None
        assert mapped[0].best.source_chunk_id == "s1"
        assert mapped[0].best.similarity == 1.0

    def test_the_nearest_source_passage_wins(self):
        doc = [passage("d1", vec=unit(1.0, 0.1))]
        src = [passage("far", vec=unit(0.0, 1.0)), passage("near", vec=unit(1.0, 0.0))]
        assert ProvenanceMapper.map(doc, src)[0].best.source_chunk_id == "near"

    def test_a_tie_resolves_the_same_way_every_call(self):
        # A highlight that moves between reloads reads as a bug.
        v = unit(1.0, 0.0)
        doc = [passage("d1", vec=v)]
        src = [passage("s2", vec=v), passage("s1", vec=v)]
        for _ in range(5):
            assert ProvenanceMapper.map(doc, src)[0].best.source_chunk_id == "s1"

    def test_it_carries_the_source_lines_so_the_ui_can_point_at_them(self):
        v = unit(1.0, 0.0)
        src = [passage("s1", vec=v, lf=12, lt=15, text="Auth is a signed JWT.")]
        best = ProvenanceMapper.map([passage("d1", vec=v)], src)[0].best
        assert (best.source_line_from, best.source_line_to) == (12, 15)
        assert best.source_text == "Auth is a signed JWT."


class TestWhatChanged:
    def test_a_match_inside_the_changed_lines_is_flagged(self):
        # The whole point: not "built on a source that moved" but
        # "built on the part of it that moved".
        v = unit(1.0, 0.0)
        doc = [passage("d1", vec=v)]
        src = [passage("s1", vec=v, lf=10, lt=12)]

        mapped = ProvenanceMapper.map(doc, src, changed=[(11, 11)])

        assert mapped[0].best.changed is True
        assert mapped[0].affected is True

    def test_a_match_outside_them_is_not(self):
        v = unit(1.0, 0.0)
        doc = [passage("d1", vec=v)]
        src = [passage("s1", vec=v, lf=10, lt=12)]

        mapped = ProvenanceMapper.map(doc, src, changed=[(40, 42)])

        assert mapped[0].best.changed is False
        assert mapped[0].affected is False

    def test_with_no_diff_supplied_nothing_is_called_changed(self):
        v = unit(1.0, 0.0)
        mapped = ProvenanceMapper.map([passage("d1", vec=v)], [passage("s1", vec=v)])
        assert mapped[0].affected is False


class TestReadingTheDiff:
    def test_equal_rows_contribute_nothing(self):
        rows = [DiffRow(kind=RowKind.EQUAL, left_n=1, right_n=1, left="a", right="a")]
        assert ProvenanceMapper.changed_ranges(rows) == []

    def test_changed_added_and_removed_rows_all_count(self):
        rows = [
            DiffRow(kind=RowKind.CHANGED, left_n=1, right_n=1, left="a", right="b"),
            DiffRow(kind=RowKind.ADDED, left_n=0, right_n=5, left="", right="c"),
        ]
        assert ProvenanceMapper.changed_ranges(rows) == [(1, 1), (5, 5)]

    def test_a_removed_row_with_no_right_line_is_skipped(self):
        # It has no line in the version the source is at now, so there
        # is nothing on that side for a passage to overlap.
        rows = [DiffRow(kind=RowKind.REMOVED, left_n=3, right_n=0, left="gone", right="")]
        assert ProvenanceMapper.changed_ranges(rows) == []

    def test_consecutive_changed_lines_collapse_into_one_edit(self):
        rows = [
            DiffRow(kind=RowKind.CHANGED, left_n=i, right_n=i, left="a", right="b")
            for i in (4, 5, 6)
        ]
        assert ProvenanceMapper.changed_ranges(rows) == [(4, 6)]

    def test_separated_edits_stay_separate(self):
        rows = [
            DiffRow(kind=RowKind.CHANGED, left_n=1, right_n=1, left="a", right="b"),
            DiffRow(kind=RowKind.CHANGED, left_n=9, right_n=9, left="a", right="b"),
        ]
        assert ProvenanceMapper.changed_ranges(rows) == [(1, 1), (9, 9)]


class TestPassageOverlap:
    def test_a_passage_touching_the_edge_of_a_range_counts(self):
        assert passage("p", lf=5, lt=9).touches(9, 12) is True
        assert passage("p", lf=5, lt=9).touches(1, 5) is True

    def test_a_passage_clear_of_it_does_not(self):
        assert passage("p", lf=5, lt=9).touches(10, 12) is False
        assert passage("p", lf=5, lt=9).touches(1, 4) is False


class TestTheFloorIsDeliberate:
    def test_it_sits_above_the_noise_that_was_measured(self):
        # Not a guess. On a real store, passages sharing only a topic
        # scored 0.12-0.56. Below this the whole document reads as
        # derived from the whole source, which says nothing.
        assert MEASURED_NOISE_CEILING < FLOOR

    def test_and_below_the_weakest_real_derivation_measured(self):
        # The same store's genuine derivations scored 0.74 and 0.75.
        # Above this they would be dropped and the feature would show
        # an empty rail on a document that plainly has a source.
        assert FLOOR < MEASURED_SIGNAL_FLOOR

    def test_the_gap_is_wide_enough_to_be_worth_a_threshold(self):
        # If noise and signal overlapped, no floor would separate them
        # and the honest move would be to not draw the lines at all.
        assert MEASURED_SIGNAL_FLOOR - MEASURED_NOISE_CEILING > 0.1


class TestTheShapeTheUiReads:
    def test_affected_is_true_when_any_match_changed(self):
        p = PassageProvenance(
            chunk_id="d1",
            line_from=1,
            line_to=2,
            text="x",
            matches=[
                Match("s1", 1, 1, "a", 0.9, changed=False),
                Match("s2", 4, 4, "b", 0.8, changed=True),
            ],
        )
        assert p.affected is True
        assert p.best.source_chunk_id == "s1"


class TestQuotingASourcePassage:
    """A match points at lines; the quote has to be those lines.

    Chunks are cut to a character budget, so a chunk's text begins and
    ends wherever the budget ran out. Quoting it raw produced cards
    opening mid-sentence — "of the caller's budget:" — with a bare
    ``` sitting on a line of its own.
    """

    DOC = "\n".join(
        [
            "# Rate limiting",  # 1
            "",  # 2
            "Headers on every response describe the rest",  # 3
            "of the caller's budget:",  # 4
            "",  # 5
            "```",  # 6
            "RateLimit-Limit: 120",  # 7
            "RateLimit-Remaining: 84",  # 8
            "```",  # 9
            "",  # 10
            "`RateLimit-Reset` is seconds remaining.",  # 11
        ]
    )

    def test_it_quotes_whole_lines(self):
        assert quote_lines(self.DOC, 3, 4).startswith("Headers on every response")

    def test_a_range_ending_inside_a_fence_gets_it_closed(self):
        quote = quote_lines(self.DOC, 1, 7)
        assert quote.count("```") == 2
        assert quote.rstrip().endswith("```")

    def test_a_range_starting_inside_a_fence_gets_it_opened(self):
        # The direction that cannot be read off the slice: ``` both
        # opens and closes, so only the document knows which this is.
        # Getting it backwards renders the block as prose, and markdown
        # eats the underscores in names like ``access_ttl_minutes``.
        quote = quote_lines(self.DOC, 7, 11)
        assert quote.startswith("```\n")
        assert quote.count("```") == 2
        assert "RateLimit-Limit: 120" in quote

    def test_a_balanced_range_is_left_alone(self):
        assert quote_lines(self.DOC, 6, 9).count("```") == 2

    def test_a_range_past_the_end_is_clipped(self):
        assert quote_lines(self.DOC, 10, 400).endswith("seconds remaining.")

    def test_a_range_that_lands_nowhere_says_so(self):
        # Empty, not a guess — the caller keeps whatever it had.
        assert quote_lines(self.DOC, 99, 120) == ""
        assert quote_lines("", 1, 3) == ""

    def test_tildes_count_as_fences_too(self):
        doc = "~~~\nbody\n~~~\nafter"
        assert quote_lines(doc, 2, 4).startswith("~~~\n") or quote_lines(doc, 2, 4).startswith(
            "```\n"
        )


class TestQuotingAcrossATable:
    """A table cut below its header is not a table at all."""

    DOC = "\n".join(
        [
            "## Budgeting",  # 1
            "",  # 2
            "| Endpoint group | anonymous | authenticated |",  # 3
            "| --- | --- | --- |",  # 4
            "| reads | 120 / min | 1200 / min |",  # 5
            "| writes | 10 / min | 120 / min |",  # 6
            "",  # 7
            "After the table.",  # 8
        ]
    )

    def test_a_slice_below_the_header_gets_it_back(self):
        quote = quote_lines(self.DOC, 6, 6)
        assert quote.splitlines()[:2] == [
            "| Endpoint group | anonymous | authenticated |",
            "| --- | --- | --- |",
        ]
        assert quote.splitlines()[-1] == "| writes | 10 / min | 120 / min |"

    def test_a_slice_that_already_has_the_header_is_left_alone(self):
        quote = quote_lines(self.DOC, 3, 5)
        assert quote.count("| Endpoint group") == 1

    def test_prose_after_a_table_is_not_given_a_header(self):
        assert quote_lines(self.DOC, 8, 8) == "After the table."

    def test_a_pipe_inside_a_code_block_is_just_a_pipe(self):
        # A fence wins: repairing a "table" there would inject two
        # header rows into somebody's shell command.
        doc = "```\na | b\nc | d\n```\n"
        quote = quote_lines(doc, 3, 4)
        assert quote.startswith("```\n")
        assert "---" not in quote


class TestSayingHowSure:
    """The bands, cut against the measurement behind ``FLOOR``.

    Recorded there: passages sharing only a topic scored 0.12–0.56,
    and genuine derivations scored 0.74 and 0.75.
    """

    #: The two real derivations the floor was set from.
    MEASURED_DERIVATIONS = (0.74, 0.75)
    #: The top of the topic-only noise in the same sample.
    MEASURED_NOISE_CEILING = 0.56

    def test_a_measured_derivation_is_not_in_the_weakest_band(self):
        # The bug this recut fixes. Both real matches ever measured
        # scored 0.74–0.75, and the old cut at 0.80 called them
        # "loose" — the lowest grade on a scale, handed to the only
        # known-correct answers.
        for score in self.MEASURED_DERIVATIONS:
            assert strength_of(score) == "close", score

    def test_the_noise_never_reaches_the_floor(self):
        # If it did, the bands would be grading noise rather than
        # deciding between real matches.
        assert self.MEASURED_NOISE_CEILING < FLOOR

    def test_the_weak_band_sits_between_the_evidence(self):
        # Above every noise score measured, below every genuine one:
        # the strip where we genuinely do not know.
        assert strength_of(FLOOR) == "weak"
        assert strength_of(0.73) == "weak"
        assert min(self.MEASURED_DERIVATIONS) == CLOSE

    def test_it_is_called_weak_rather_than_loose(self):
        # "Loose" describes the match; "weak" describes the evidence
        # for it, which is the thing that is actually thin down here.
        assert "loose" not in {strength_of(s) for s in (0.70, 0.80, 0.95)}

    def test_near_restatement_is_still_its_own_band(self):
        assert strength_of(VERY_CLOSE) == "very close"
        assert strength_of(0.89) == "close"

    def test_the_bands_are_ordered_and_total(self):
        labels = [strength_of(s / 100) for s in range(70, 101)]
        assert set(labels) == {"weak", "close", "very close"}
        # Monotonic: a higher score never gets a weaker word.
        rank = {"weak": 0, "close": 1, "very close": 2}
        assert labels == sorted(labels, key=lambda w: rank[w])
