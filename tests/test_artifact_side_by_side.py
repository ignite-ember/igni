"""Two versions, aligned into columns.

The unified diff that already existed is the right shape for a model
and the wrong one for a person: it interleaves the two texts, so "what
did this paragraph used to say" means scanning up and down for the
matching ``-`` line. These rows sit the versions side by side instead.

The behaviour worth defending is the pairing. A rewritten line has to
come out as **one row with two halves**, not as a deletion and an
insertion several rows apart — that is the whole reason the alignment
uses opcodes rather than zipping two lists together, and it is the
case a naive implementation gets wrong while looking right on
append-only edits.
"""

from __future__ import annotations

from ember_code.core.artifacts.side_by_side import RowKind, build


def kinds(diff) -> list[str]:
    return [row.kind.value for row in diff.rows]


class TestUnchangedText:
    def test_identical_versions_produce_only_equal_rows(self):
        diff = build("one\ntwo", "one\ntwo")
        assert kinds(diff) == ["equal", "equal"]

    def test_and_report_themselves_as_unchanged(self):
        """A revision that only touched metadata has an empty diff, and
        "no changes" beats a blank panel."""
        assert build("same", "same").unchanged

    def test_both_sides_carry_their_own_line_numbers(self):
        row = build("one", "one").rows[0]
        assert (row.left_n, row.right_n) == (1, 1)


class TestARewrittenLineIsOneRow:
    """The case the pairing exists for."""

    def test_a_replacement_pairs_both_sides(self):
        diff = build("cookie lasts 24 hours", "JWT lasts 15 minutes")
        assert kinds(diff) == ["changed"]
        assert diff.rows[0].left == "cookie lasts 24 hours"
        assert diff.rows[0].right == "JWT lasts 15 minutes"

    def test_it_counts_as_changed_not_as_an_add_and_a_delete(self):
        diff = build("before", "after")
        assert (diff.changed, diff.added, diff.removed) == (1, 0, 0)

    def test_an_uneven_replacement_pairs_what_it_can(self):
        # Two lines become three: two pair up, the leftover is an add.
        diff = build("a\nb", "A\nB\nC")
        assert kinds(diff) == ["changed", "changed", "added"]
        assert diff.rows[2].left == ""
        assert diff.rows[2].right == "C"

    def test_and_the_other_way_round(self):
        diff = build("a\nb\nc", "A\nB")
        assert kinds(diff) == ["changed", "changed", "removed"]
        assert diff.rows[2].right == ""


class TestAddedAndRemoved:
    def test_an_appended_line_is_an_addition_with_no_left_side(self):
        diff = build("one", "one\ntwo")
        assert kinds(diff) == ["equal", "added"]
        assert diff.rows[1].left == ""
        assert diff.rows[1].left_n == 0

    def test_a_deleted_line_has_no_right_side(self):
        diff = build("one\ntwo", "one")
        assert kinds(diff) == ["equal", "removed"]
        assert diff.rows[1].right == ""
        assert diff.rows[1].right_n == 0

    def test_line_numbers_stay_true_to_their_own_version(self):
        """A row's position in the list stops being its line number the
        moment anything is added, which is why both numbers travel."""
        diff = build("a\nc", "a\nb\nc")
        equal_rows = [r for r in diff.rows if r.kind is RowKind.EQUAL]
        last = equal_rows[-1]
        assert (last.left_n, last.right_n) == (2, 3)

    def test_the_counts_add_up(self):
        diff = build("keep\ndrop", "keep\nadd\nmore")
        assert diff.added + diff.removed + diff.changed == 2


class TestTheRealisticCase:
    """The eval fixture's revision, which is what this is for."""

    BEFORE = (
        "The login flow posts credentials, then stores the session cookie.\n"
        "The cookie is good for 24 hours.\n"
        "\n"
        "On failure the flow surfaces `auth.rejected`."
    )
    AFTER = (
        "The login flow posts credentials, then stores the signed JWT.\n"
        "It expires in 15 minutes.\n"
        "\n"
        "On failure the flow surfaces `auth.rejected`.\n"
        "Rate limiting is per-IP at the edge."
    )

    def test_the_rewritten_opening_pairs_and_the_new_line_appends(self):
        diff = build(self.BEFORE, self.AFTER)
        assert kinds(diff) == ["changed", "changed", "equal", "equal", "added"]

    def test_the_untouched_paragraph_stays_untouched(self):
        diff = build(self.BEFORE, self.AFTER)
        unchanged = [r.left for r in diff.rows if r.kind is RowKind.EQUAL]
        assert "On failure the flow surfaces `auth.rejected`." in unchanged

    def test_it_is_not_reported_as_unchanged(self):
        assert not build(self.BEFORE, self.AFTER).unchanged


class TestTruncation:
    def test_a_huge_diff_is_capped(self):
        diff = build("\n".join(str(n) for n in range(50)), "", max_rows=10)
        assert len(diff.rows) == 10

    def test_and_says_how_much_it_dropped(self):
        """A diff that quietly stops reads as "and nothing else
        changed", which is the one thing it must not say."""
        diff = build("\n".join(str(n) for n in range(50)), "", max_rows=10)
        assert diff.truncated == 40

    def test_the_counts_describe_the_whole_diff_not_the_visible_part(self):
        diff = build("\n".join(str(n) for n in range(50)), "", max_rows=10)
        assert diff.removed == 50


class TestEdges:
    def test_empty_against_empty_is_unchanged(self):
        assert build("", "").unchanged

    def test_a_first_version_is_all_additions(self):
        diff = build("", "one\ntwo")
        assert kinds(diff) == ["added", "added"]

    def test_deleting_everything_is_all_removals(self):
        diff = build("one\ntwo", "")
        assert kinds(diff) == ["removed", "removed"]

    def test_trailing_newlines_do_not_invent_a_row(self):
        # splitlines() rather than keepends: the terminator is not
        # something a reader is comparing.
        assert len(build("one\n", "one\n").rows) == 1

    def test_blank_lines_are_preserved_as_rows(self):
        # Paragraph breaks are meaningful in prose; collapsing them
        # would misalign everything after the first one.
        diff = build("a\n\nb", "a\n\nb")
        assert len(diff.rows) == 3

    def test_labels_travel_with_the_diff(self):
        diff = build("a", "b", artifact_id="abc", name="Login flow", from_n=2, to_n=3)
        assert (diff.name, diff.from_n, diff.to_n) == ("Login flow", 2, 3)
