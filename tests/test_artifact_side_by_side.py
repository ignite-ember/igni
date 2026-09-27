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


def lines(before: str, after: str, **kw):
    """Compare line by line.

    The default is paragraphs, which is right for documents and wrong
    for testing the alignment mechanics — under it, "one\ntwo" is a
    single paragraph and there is no second row to assert about.
    """
    return build(before, after, by_paragraph=False, **kw)


def kinds(diff) -> list[str]:
    return [row.kind.value for row in diff.rows]


class TestUnchangedText:
    def test_identical_versions_produce_only_equal_rows(self):
        diff = lines("one\ntwo", "one\ntwo")
        assert kinds(diff) == ["equal", "equal"]

    def test_and_report_themselves_as_unchanged(self):
        """A revision that only touched metadata has an empty diff, and
        "no changes" beats a blank panel."""
        assert lines("same", "same").unchanged

    def test_both_sides_carry_their_own_line_numbers(self):
        row = lines("one", "one").rows[0]
        assert (row.left_n, row.right_n) == (1, 1)


class TestARewrittenLineIsOneRow:
    """The case the pairing exists for."""

    def test_a_replacement_pairs_both_sides(self):
        diff = lines("cookie lasts 24 hours", "JWT lasts 15 minutes")
        assert kinds(diff) == ["changed"]
        assert diff.rows[0].left == "cookie lasts 24 hours"
        assert diff.rows[0].right == "JWT lasts 15 minutes"

    def test_it_counts_as_changed_not_as_an_add_and_a_delete(self):
        diff = lines("before", "after")
        assert (diff.changed, diff.added, diff.removed) == (1, 0, 0)

    def test_an_uneven_replacement_pairs_what_it_can(self):
        # Two lines become three: two pair up, the leftover is an add.
        diff = lines("a\nb", "A\nB\nC")
        assert kinds(diff) == ["changed", "changed", "added"]
        assert diff.rows[2].left == ""
        assert diff.rows[2].right == "C"

    def test_and_the_other_way_round(self):
        diff = lines("a\nb\nc", "A\nB")
        assert kinds(diff) == ["changed", "changed", "removed"]
        assert diff.rows[2].right == ""


class TestAddedAndRemoved:
    def test_an_appended_line_is_an_addition_with_no_left_side(self):
        diff = lines("one", "one\ntwo")
        assert kinds(diff) == ["equal", "added"]
        assert diff.rows[1].left == ""
        assert diff.rows[1].left_n == 0

    def test_a_deleted_line_has_no_right_side(self):
        diff = lines("one\ntwo", "one")
        assert kinds(diff) == ["equal", "removed"]
        assert diff.rows[1].right == ""
        assert diff.rows[1].right_n == 0

    def test_line_numbers_stay_true_to_their_own_version(self):
        """A row's position in the list stops being its line number the
        moment anything is added, which is why both numbers travel."""
        diff = lines("a\nc", "a\nb\nc")
        equal_rows = [r for r in diff.rows if r.kind is RowKind.EQUAL]
        last = equal_rows[-1]
        assert (last.left_n, last.right_n) == (2, 3)

    def test_the_counts_add_up(self):
        diff = lines("keep\ndrop", "keep\nadd\nmore")
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
        diff = lines(self.BEFORE, self.AFTER)
        assert kinds(diff) == ["changed", "changed", "equal", "equal", "added"]

    def test_the_untouched_paragraph_stays_untouched(self):
        diff = lines(self.BEFORE, self.AFTER)
        unchanged = [r.left for r in diff.rows if r.kind is RowKind.EQUAL]
        assert "On failure the flow surfaces `auth.rejected`." in unchanged

    def test_it_is_not_reported_as_unchanged(self):
        assert not lines(self.BEFORE, self.AFTER).unchanged


class TestTruncation:
    def test_a_huge_diff_is_capped(self):
        diff = lines("\n".join(str(n) for n in range(50)), "", max_rows=10)
        assert len(diff.rows) == 10

    def test_and_says_how_much_it_dropped(self):
        """A diff that quietly stops reads as "and nothing else
        changed", which is the one thing it must not say."""
        diff = lines("\n".join(str(n) for n in range(50)), "", max_rows=10)
        assert diff.truncated == 40

    def test_the_counts_describe_the_whole_diff_not_the_visible_part(self):
        diff = lines("\n".join(str(n) for n in range(50)), "", max_rows=10)
        assert diff.removed == 50


class TestEdges:
    def test_empty_against_empty_is_unchanged(self):
        assert lines("", "").unchanged

    def test_a_first_version_is_all_additions(self):
        diff = lines("", "one\ntwo")
        assert kinds(diff) == ["added", "added"]

    def test_deleting_everything_is_all_removals(self):
        diff = lines("one\ntwo", "")
        assert kinds(diff) == ["removed", "removed"]

    def test_trailing_newlines_do_not_invent_a_row(self):
        # splitlines() rather than keepends: the terminator is not
        # something a reader is comparing.
        assert len(lines("one\n", "one\n").rows) == 1

    def test_blank_lines_are_preserved_as_rows(self):
        # Paragraph breaks are meaningful in prose; collapsing them
        # would misalign everything after the first one.
        diff = lines("a\n\nb", "a\n\nb")
        assert len(diff.rows) == 3

    def test_labels_travel_with_the_diff(self):
        diff = lines("a", "b", artifact_id="abc", name="Login flow", from_n=2, to_n=3)
        assert (diff.name, diff.from_n, diff.to_n) == ("Login flow", 2, 3)


class TestWordLevelChanges:
    """Marking the whole line is most of the work left undone.

    Prose is edited *inside* a sentence. A line-level mark tells a
    reader something in this paragraph moved and leaves them to find
    out what — which for a paragraph is the hard part. A code diff can
    get away with it because a changed line of code is usually short
    and read whole; a changed line of prose is not.
    """

    def test_a_rewritten_line_marks_only_the_words_that_differ(self):
        diff = build("The cookie is good for 24 hours.", "The token is good for 15 hours.")
        row = diff.rows[0]
        assert [p.text for p in row.left_parts if p.changed] == ["cookie", "24"]
        assert [p.text for p in row.right_parts if p.changed] == ["token", "15"]

    def test_the_untouched_words_are_not_marked(self):
        diff = build("keep this word", "keep that word")
        unchanged = "".join(p.text for p in diff.rows[0].right_parts if not p.changed)
        assert "keep" in unchanged and "word" in unchanged

    def test_the_parts_rebuild_the_line_exactly(self):
        # They are rendered as spans of one paragraph, so anything lost
        # or added here changes the sentence on screen.
        before = "Auth is a session cookie, set on sign-in."
        after = "Auth is a signed JWT, set on sign-in."
        row = build(before, after).rows[0]
        assert "".join(p.text for p in row.left_parts) == before
        assert "".join(p.text for p in row.right_parts) == after

    def test_whitespace_alone_is_never_marked(self):
        # A changed word sits between two spaces. Marking those spaces
        # stretches the highlight past the word on both sides, which
        # reads as though the punctuation changed too.
        row = build("one two three", "one four three").rows[0]
        assert all(p.text.strip() for p in row.right_parts if p.changed)

    def test_neighbours_that_agree_are_merged(self):
        # Fewer spans for the renderer, and no seams mid-phrase.
        row = build("a b c d", "a x c d").rows[0]
        kinds = [p.changed for p in row.right_parts]
        assert kinds == sorted(set(kinds), key=kinds.index) or len(kinds) == len(
            set([(i, k) for i, k in enumerate(kinds)])
        )
        # No two adjacent parts share a flag.
        assert all(a.changed != b.changed for a, b in zip(row.right_parts, row.right_parts[1:]))

    def test_an_added_passage_has_no_parts(self):
        # Nothing to compare it against on the other side. A blank
        # line, because under paragraph comparison "one\ntwo" is one
        # paragraph that grew, not two lines.
        diff = build("one", "one\n\ntwo")
        added = next(r for r in diff.rows if r.kind is RowKind.ADDED)
        assert added.left_parts == [] and added.right_parts == []

    def test_an_equal_line_has_no_parts_either(self):
        diff = build("same", "same")
        assert diff.rows[0].left_parts == [] and diff.rows[0].right_parts == []

    def test_a_wholly_rewritten_passage_is_not_marked_word_by_word(self):
        """Past the floor, every word would light up and the reader is
        back to comparing two highlighted paragraphs by eye."""
        row = build("completely different words here", "nothing at all alike").rows[0]
        assert row.left_parts == [] and row.right_parts == []

    def test_but_a_small_edit_still_is(self):
        row = build("The cookie is good for 24 hours.", "The cookie is good for 15 hours.").rows[0]
        assert [p.text for p in row.right_parts if p.changed] == ["15"]


class TestParagraphsAreTheUnit:
    """Why a document diff cannot be a line diff.

    Prose is hard-wrapped. Edit one sentence and every line after it
    re-flows, so a line-based comparison reports a whole paragraph as
    changed when one word moved — and pairs "line 2 of the old"
    against "line 2 of the new" even though they are now different
    sentences. Measured on the store's own Auth model: a two-word edit
    lit up 35 words across four rows.

    Paragraphs are stable under re-wrapping, so they are the unit
    people actually edited in.
    """

    WRAPPED_NARROW = "Auth is a session cookie, set on\nsign-in and read on every request."
    WRAPPED_WIDE = "Auth is a session cookie, set on sign-in and read on every request."

    def test_rewrapping_alone_is_not_a_change(self):
        assert build(self.WRAPPED_NARROW, self.WRAPPED_WIDE).unchanged

    def test_the_same_text_wrapped_differently_is_one_row(self):
        diff = build(self.WRAPPED_NARROW, self.WRAPPED_WIDE)
        assert kinds(diff) == ["equal"]

    def test_line_mode_would_have_called_it_two_changes(self):
        # The contrast, stated: this is what the panel used to show.
        diff = lines(self.WRAPPED_NARROW, self.WRAPPED_WIDE)
        assert diff.changed > 0

    def test_a_blank_line_separates_paragraphs(self):
        diff = build("one\n\ntwo", "one\n\ntwo")
        assert len(diff.rows) == 2

    def test_several_blank_lines_do_not_make_empty_paragraphs(self):
        diff = build("one\n\n\n\ntwo", "one\n\ntwo")
        assert len(diff.rows) == 2 and diff.unchanged

    def test_an_edit_inside_a_wrapped_paragraph_marks_only_the_edit(self):
        before = "The cookie is good for 24 hours, so a\nreturning user skips the form."
        after = "The cookie is good for 15 minutes, so a\nreturning user skips the form."
        row = build(before, after).rows[0]
        assert [p.text for p in row.right_parts if p.changed] == ["15 minutes,"]

    def test_trailing_whitespace_does_not_invent_a_paragraph(self):
        assert len(build("one\n\n", "one").rows) == 1


class TestHighlightsAreContiguous:
    def test_a_replaced_phrase_is_one_highlight_not_two(self):
        """ "24 hours" -> "15 minutes" is two changed words with a space
        between them. Marked separately they draw two boxes with a gap,
        which reads as two unrelated edits rather than one phrase."""
        row = build("good for 24 hours", "good for 15 minutes").rows[0]
        assert [p.text for p in row.right_parts if p.changed] == ["15 minutes"]

    def test_a_space_between_unchanged_words_is_still_not_marked(self):
        row = build("one two three four", "one two THREE four").rows[0]
        marked = [p.text for p in row.right_parts if p.changed]
        assert marked == ["THREE"]

    def test_the_parts_still_rebuild_the_passage(self):
        before, after = "good for 24 hours today", "good for 15 minutes today"
        row = build(before, after).rows[0]
        assert "".join(p.text for p in row.right_parts) == after
        assert "".join(p.text for p in row.left_parts) == before


class TestStructuredContent:
    """A document is not only prose.

    Bullets, numbered steps, table rows and code have no blank lines
    between them, so splitting on blank lines alone swallowed each
    list, each table and each fenced block into one line. "Added one
    bullet" then rendered as a rewritten paragraph, and changing one
    key in a YAML block rewrote the whole block.

    Verified against a long document carrying all four: 20 rows before,
    39 after, and the edits landed on the items somebody actually
    edited.
    """

    def test_a_bullet_added_is_an_addition_not_a_rewrite(self):
        before = "- one\n- two"
        after = "- one\n- one and a half\n- two"
        diff = build(before, after)
        assert kinds(diff) == ["equal", "added", "equal"]

    def test_a_bullet_removed_is_a_removal(self):
        diff = build("- one\n- two\n- three", "- one\n- three")
        assert kinds(diff) == ["equal", "removed", "equal"]

    def test_numbered_steps_compare_one_by_one(self):
        before = "1. first\n2. second\n3. third"
        after = "1. first\n2. changed\n3. third"
        diff = build(before, after)
        assert kinds(diff) == ["equal", "changed", "equal"]

    def test_a_list_item_that_wraps_stays_one_item(self):
        # The continuation line belongs to the bullet above it, not to
        # a bullet of its own.
        before = "- a long bullet that\n  wraps onto a second line\n- another"
        after = "- a long bullet that wraps onto a second line\n- another"
        assert build(before, after).unchanged

    def test_headings_are_their_own_block(self):
        # A heading has no blank line after it in most documents, and
        # joining it to the paragraph below turns an edited sentence
        # into an edited section title.
        before = "## Title\nSome prose here."
        after = "## Title\nDifferent prose here."
        diff = build(before, after)
        assert kinds(diff) == ["equal", "changed"]

    def test_code_inside_a_fence_compares_line_by_line(self):
        before = "```yaml\nttl: 24\nname: sid\n```"
        after = "```yaml\nttl: 15\nname: sid\n```"
        diff = build(before, after)
        assert kinds(diff) == ["equal", "changed", "equal", "equal"]

    def test_code_indentation_is_preserved(self):
        # Inside a fence the indentation *is* the content.
        diff = build("```\n    indented\n```", "```\n    indented\n```")
        assert diff.rows[1].left == "    indented"

    def test_blank_lines_inside_a_fence_are_kept(self):
        # A blank line in code is a line, not a paragraph break.
        diff = build("```\na\n\nb\n```", "```\na\n\nb\n```")
        assert len(diff.rows) == 5

    def test_a_fence_that_is_never_closed_does_not_swallow_the_rest(self):
        # Malformed input should degrade, not lose the document.
        diff = build("```\ncode\nmore", "```\ncode\nmore")
        assert diff.unchanged and len(diff.rows) == 3

    def test_table_rows_compare_one_by_one(self):
        before = "| a | 1 |\n| b | 2 |"
        after = "| a | 1 |\n| b | 3 |"
        diff = build(before, after)
        assert kinds(diff) == ["equal", "changed"]

    def test_prose_is_still_joined_across_a_soft_wrap(self):
        # The original reason for blocks: re-wrapping is not an edit.
        assert build("one two\nthree four", "one two three four").unchanged


class TestBlockquotes:
    def test_a_quote_is_one_block_without_stray_markers(self):
        """Joining the lines verbatim left every marker after the first
        sitting mid-sentence: "...what somebody > did, read the audit
        log". Markdown consumes the first one and renders the rest."""
        from ember_code.core.artifacts.side_by_side import _blocks

        quote = "> one line\n> and the next\n> and the last"
        assert _blocks(quote) == ["> one line and the next and the last"]

    def test_rewrapping_a_quote_is_not_a_change(self):
        before = "> Sessions are not an audit trail. If you\n> need to know, read the log."
        after = "> Sessions are not an audit trail.\n> If you need to know, read the log."
        assert build(before, after).unchanged

    def test_prose_under_a_quote_is_not_part_of_it(self):
        from ember_code.core.artifacts.side_by_side import _blocks

        assert _blocks("> quoted\nnot quoted") == ["> quoted", "not quoted"]

    def test_a_quote_after_prose_starts_its_own_block(self):
        from ember_code.core.artifacts.side_by_side import _blocks

        assert _blocks("prose\n> quoted") == ["prose", "> quoted"]

    def test_an_edit_inside_a_quote_marks_only_the_edit(self):
        before = "> good for 24 hours\n> in every case"
        after = "> good for 15 minutes\n> in every case"
        row = build(before, after).rows[0]
        assert [p.text for p in row.right_parts if p.changed] == ["15 minutes"]
