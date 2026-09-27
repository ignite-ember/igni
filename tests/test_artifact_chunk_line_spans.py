"""Where a chunk sits in the document it came from.

Every citation points at a line range, so this is the thing they all
rest on. The hard part is that the chunker *normalises* as it splits —
blank-line runs collapse, space runs collapse — so a chunk's text is
usually not a literal substring of the document it came from.

The version of this that searched for the text verbatim missed on
those chunks and returned ``(0, 0)``. That is the failure worth
guarding: a zero sorts to the top of the document and points at a line
that does not exist, and neither looks like an error from the outside.
"""

from __future__ import annotations

from ember_code.core.artifacts.index import _normalise_with_offsets


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


class TestTheOffsetMap:
    def test_every_normalised_character_maps_home(self):
        text = "a  b\n\n\n\nc"
        flat, offsets = _normalise_with_offsets(text)
        assert len(flat) == len(offsets)
        assert all(0 <= o < len(text) for o in offsets)

    def test_a_run_of_spaces_collapses_to_one(self):
        flat, _ = _normalise_with_offsets("a     b")
        assert flat == "a b"

    def test_tabs_collapse_with_spaces(self):
        flat, _ = _normalise_with_offsets("a \t \t b")
        assert flat == "a b"

    def test_three_or_more_newlines_cap_at_two(self):
        flat, _ = _normalise_with_offsets("a\n\n\n\n\nb")
        assert flat == "a\n\nb"

    def test_two_newlines_are_left_alone(self):
        flat, _ = _normalise_with_offsets("a\n\nb")
        assert flat == "a\n\nb"

    def test_windows_line_endings_become_one_newline(self):
        flat, _ = _normalise_with_offsets("a\r\nb")
        assert flat == "a\nb"

    def test_a_bare_carriage_return_becomes_one_newline(self):
        flat, _ = _normalise_with_offsets("a\rb")
        assert flat == "a\nb"

    def test_empty_text_still_yields_a_usable_map(self):
        # Indexing into an empty offsets list is how this turns into a
        # crash rather than a missing citation.
        flat, offsets = _normalise_with_offsets("")
        assert flat == ""
        assert offsets == [0]


class TestFindingAChunkAgain:
    def test_a_chunk_normalised_away_from_its_source_still_locates(self):
        # The case the old code got wrong: the document has four blank
        # lines, the chunk has one, so the chunk is not a substring.
        document = "# Title\n\n\n\n\nThe  body  text.\n"
        chunk = "The body text."

        flat, offsets = _normalise_with_offsets(document)
        needle, _ = _normalise_with_offsets(chunk)
        found = flat.find(needle)

        assert found >= 0, "normalising both sides is what makes this findable"
        assert document.find(chunk) < 0, "and it genuinely is not a literal substring"

    def test_the_line_it_reports_is_the_line_it_is_on(self):
        document = "one\ntwo\n\n\n\nthree  four\nfive\n"
        flat, offsets = _normalise_with_offsets(document)
        needle, _ = _normalise_with_offsets("three four")

        start = offsets[flat.find(needle)]

        assert document[start:].startswith("three")
        assert line_of(document, start) == 6

    def test_a_multi_line_chunk_reports_both_ends(self):
        document = "intro\n\nalpha\nbeta\ngamma\n\nouttro\n"
        flat, offsets = _normalise_with_offsets(document)
        needle, _ = _normalise_with_offsets("alpha\nbeta\ngamma")
        found = flat.find(needle)

        start = offsets[found]
        last = offsets[found + len(needle) - 1]

        assert line_of(document, start) == 3
        assert line_of(document, last) == 5

    def test_a_later_chunk_does_not_match_an_earlier_one(self):
        # Two passages that read alike is ordinary; the cursor is what
        # stops the second being placed on top of the first.
        document = "same words here\n\nfiller\n\nsame words here\n"
        flat, offsets = _normalise_with_offsets(document)
        needle, _ = _normalise_with_offsets("same words here")

        first = flat.find(needle)
        second = flat.find(needle, first + max(len(needle) // 2, 1))

        assert line_of(document, offsets[first]) == 1
        assert line_of(document, offsets[second]) == 5
