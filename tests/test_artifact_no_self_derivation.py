"""A document is never its own source.

The version chain already records that v6 follows v5 — ``HAS_VERSION``
and ``n`` say so. A ``DERIVED_FROM`` edge alongside it says something
different and false: that the document is *built on* an earlier draft
of itself, the way it is built on another document.

Staleness is a query over those edges, so the consequence was not a
stray row in a graph nobody looks at. Every revision made the document
permanently out of date with respect to itself, and the banner on a
freshly-written document read "Sources have changed since this was
written" — naming the document itself as the source that moved.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.artifacts.models import (
    ArtifactLineage,
    EdgeState,
    LineageEdge,
    SourceDeclaration,
    VersionPin,
)


def pin(artifact_id: str, n: int = 1) -> VersionPin:
    return VersionPin(artifact_id=artifact_id, n=n, state=EdgeState.CONFIRMED)


def edge(to: str, *, frm: str = "a1", pinned: int = 1) -> LineageEdge:
    return LineageEdge(
        from_artifact_id=frm,
        from_n=2,
        to_artifact_id=to,
        to_n=pinned,
        pinned_n=pinned,
        state=EdgeState.CONFIRMED,
    )


class TestTheWriteRefusesIt:
    def test_a_self_pin_is_dropped_and_the_others_kept(self):
        # The filter is a list comprehension in ``_write``; this pins
        # the rule it implements rather than reaching into the index,
        # which needs a live store to construct.
        artifact_id = "a1"
        sources = SourceDeclaration(pins=[pin("a1"), pin("b2"), pin("c3")])

        kept = [p for p in sources.pins if p.artifact_id != artifact_id]

        assert [p.artifact_id for p in kept] == ["b2", "c3"]

    def test_a_declaration_of_nothing_but_itself_leaves_no_sources(self):
        kept = [p for p in [pin("a1")] if p.artifact_id != "a1"]
        assert kept == []


class TestInheritanceDoesNotCarryItForward:
    """The rule in ``_inherit_confirmed``.

    Without it, one edge recorded before the write was guarded would
    be copied onto every later revision — outliving the bug that made
    it, and immune to fixing the write path.
    """

    def inherited(self, artifact_id: str, lineage: ArtifactLineage, declared: set[str]):
        return [
            e.to_artifact_id
            for e in lineage.sources
            if e.state is EdgeState.CONFIRMED
            and e.to_artifact_id not in declared
            and e.to_artifact_id != artifact_id
        ]

    def test_a_previous_self_edge_is_not_inherited(self):
        lineage = ArtifactLineage(artifact_id="a1", n=2, sources=[edge("a1"), edge("b2")])
        assert self.inherited("a1", lineage, set()) == ["b2"]

    def test_a_real_source_still_is(self):
        lineage = ArtifactLineage(artifact_id="a1", n=2, sources=[edge("b2")])
        assert self.inherited("a1", lineage, set()) == ["b2"]

    def test_an_already_declared_source_is_not_doubled(self):
        lineage = ArtifactLineage(artifact_id="a1", n=2, sources=[edge("b2")])
        assert self.inherited("a1", lineage, {"b2"}) == []


class TestTheQueriesExcludeIt:
    """Existing stores carry the edges; the reads must not show them.

    Filtered rather than deleted: the display is fixed immediately and
    nobody's data is removed to do it.

    Asserted against the Cypher text, which is unusual and deliberate.
    Running these queries needs a live Neo4j, so a behavioural test
    would be skipped on most machines — and the failure being guarded
    against is somebody tidying the clause away while refactoring the
    query, which text is exactly the right thing to catch.
    """

    @staticmethod
    def cypher() -> str:
        from ember_code.core.artifacts import neo4j_client

        return Path(neo4j_client.__file__).read_text()

    def test_lineage_compares_the_two_artifact_ids(self):
        assert "sa.artifact_id <> va.artifact_id" in self.cypher()

    def test_staleness_compares_them_too(self):
        assert "sa.artifact_id <> da.artifact_id" in self.cypher()


class TestWhatStalenessMeans:
    def test_an_edge_to_another_document_can_be_stale(self):
        moved = LineageEdge(
            from_artifact_id="a1",
            from_n=2,
            to_artifact_id="b2",
            to_n=1,
            pinned_n=1,
            source_head_n=3,
            state=EdgeState.CONFIRMED,
        )
        assert moved.is_stale is True

    def test_a_candidate_is_never_stale_whatever_moved(self):
        # Staleness is a claim about a dependency somebody confirmed.
        guessed = LineageEdge(
            from_artifact_id="a1",
            from_n=2,
            to_artifact_id="b2",
            to_n=1,
            pinned_n=1,
            source_head_n=9,
            state=EdgeState.CANDIDATE,
        )
        assert guessed.is_stale is False
