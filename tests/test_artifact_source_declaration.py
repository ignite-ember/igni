"""Parsing the ``sources`` argument every write is required to pass.

The declaration is the whole provenance contract in string form, so
the parse has to be unambiguous about three things: what is an
artifact reference, what is an external citation, and what is neither
(which must be reported rather than guessed at).
"""

from __future__ import annotations

from ember_code.core.artifacts.models import (
    EdgeState,
    SourceDeclaration,
    VersionPin,
)


class TestParsingRefs:
    def test_a_bare_id_pins_to_head_at_write_time(self):
        """``n=0`` is the sentinel the store resolves against head.

        An agent citing "the auth doc" should not have to know its
        version number; resolving at the write is what keeps the
        recorded pin honest rather than approximate.
        """
        pin = VersionPin.parse("abc123")
        assert pin is not None
        assert (pin.artifact_id, pin.n) == ("abc123", 0)

    def test_an_explicit_version_is_kept(self):
        pin = VersionPin.parse("abc123@4")
        assert pin is not None
        assert (pin.artifact_id, pin.n) == ("abc123", 4)

    def test_a_negative_version_is_clamped_not_rejected(self):
        pin = VersionPin.parse("abc123@-2")
        assert pin is not None
        assert pin.n == 0

    def test_a_non_numeric_suffix_is_not_a_ref(self):
        assert VersionPin.parse("abc123@latest") is None

    def test_a_url_is_not_a_ref(self):
        assert VersionPin.parse("https://example.com/doc") is None

    def test_the_ref_property_round_trips(self):
        assert VersionPin(artifact_id="x", n=7).ref == "x@7"


class TestSplittingDeclarations:
    def test_urls_become_citations_and_refs_become_pins(self):
        declaration = SourceDeclaration.from_raw(["abc@2", "https://example.com/spec", "def"])
        assert [p.ref for p in declaration.pins] == ["abc@2", "def@0"]
        assert [c.url for c in declaration.citations] == ["https://example.com/spec"]
        assert declaration.unresolved == []

    def test_declared_pins_are_confirmed_not_candidates(self):
        """An explicit citation is an assertion; capture is a guess."""
        declaration = SourceDeclaration.from_raw(["abc@2"])
        assert declaration.pins[0].state is EdgeState.CONFIRMED

    def test_unparseable_entries_are_reported_not_dropped(self):
        declaration = SourceDeclaration.from_raw(["abc@nope"])
        assert declaration.unresolved == ["abc@nope"]
        assert declaration.pins == []

    def test_blank_entries_are_ignored(self):
        declaration = SourceDeclaration.from_raw(["", "   ", "abc"])
        assert len(declaration.pins) == 1
        assert declaration.unresolved == []

    def test_an_empty_declaration_knows_it_is_empty(self):
        assert SourceDeclaration.from_raw([]).is_empty
        assert SourceDeclaration.from_raw(None).is_empty

    def test_citation_kinds_are_inferred_from_the_url(self):
        declaration = SourceDeclaration.from_raw(
            [
                "https://example.com/paper.pdf",
                "https://arxiv.org/abs/1234",
                "https://en.wikipedia.org/wiki/Thing",
                "https://youtu.be/xyz",
                "https://example.com/page",
            ]
        )
        assert [c.kind for c in declaration.citations] == [
            "pdf",
            "paper",
            "wiki",
            "video",
            "web",
        ]

    def test_a_citation_id_is_stable_for_the_same_url(self):
        first = SourceDeclaration.from_raw(["https://example.com/a"]).citations[0]
        second = SourceDeclaration.from_raw(["https://example.com/a"]).citations[0]
        assert first.source_id == second.source_id
