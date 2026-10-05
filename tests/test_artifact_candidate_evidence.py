"""What a reviewer is given before accepting or rejecting a captured read.

The review row offers Confirm and Not a source under a name and a
version number. Confirm is not a small button — it makes the edge
real, and from then on the document is reported out of date every time
that source moves — so being asked to press it knowing only that
*something* was read is how a reviewer learns to press whichever is on
the left.

These tests are about the answer being *earned*. The first cut asked
the store for passages at ``n=0``, which matches no version and
returns an empty list, so every source on earth came back "nothing in
this document resembles it". It read exactly like a finding. The rule
the tests hold: an empty overlap list is a claim about the texts, and
it is only allowed when both texts were actually read.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.backend.server_artifacts import ArtifactController
from ember_code.core.artifacts.provenance import FLOOR, Passage


def passage(chunk_id: str, text: str, line_from: int, vector: list[float]) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        text=text,
        line_from=line_from,
        line_to=line_from + 2,
        embedding=vector,
    )


class FakeClient:
    """A passage store that only answers for versions it actually has."""

    def __init__(self, by_version: dict[tuple[str, int], list[Passage]]) -> None:
        self._by_version = by_version
        self.asked: list[tuple[str, int]] = []
        #: The review kept against the edge, as the store would hold it.
        self.stored: dict[str, object] = {}
        self.writes = 0

    async def passages(self, *, artifact_id: str, n: int) -> list[Passage]:
        self.asked.append((artifact_id, n))
        # The real store matches on ``n`` exactly. ``0`` is not a
        # synonym for head, and pretending otherwise in the fake would
        # hide the bug these tests exist for.
        return list(self._by_version.get((artifact_id, n), []))

    async def edge_review(self, *, artifact_id: str, n: int, source_artifact_id: str):
        return dict(self.stored) if self.stored else None

    async def set_edge_review(
        self, *, artifact_id: str, n: int, source_artifact_id: str, review: dict
    ) -> bool:
        self.writes += 1
        self.stored.update(review)
        return True


class FakeManager:
    def __init__(self, summaries: dict, edges: list, contents: dict) -> None:
        self._summaries = summaries
        self._edges = edges
        self._contents = contents

    async def resolve_artifact(self, ref: str):
        return self._summaries.get(ref)

    async def repair_chunks(self) -> int:
        return 0

    async def backfill_chunk_embeddings(self) -> int:
        return 0

    async def lineage(self, artifact_id: str, n: int):
        return SimpleNamespace(sources=list(self._edges))

    async def version(self, artifact_id: str, n: int):
        body = self._contents.get((artifact_id, n))
        return SimpleNamespace(content=body) if body is not None else None


SAME = [1.0, 0.0, 0.0]
ALSO_SAME = [0.99, 0.14, 0.0]
UNRELATED = [0.0, 1.0, 0.0]


def controller(
    *,
    mine: list[Passage],
    theirs: list[Passage],
    mine_n: int = 3,
    read_n: int = 5,
) -> ArtifactController:
    summaries = {
        "dep": SimpleNamespace(artifact_id="dep", name="Client retry guide", head_n=mine_n),
        "src": SimpleNamespace(artifact_id="src", name="Login flow", head_n=9),
    }
    edges = [SimpleNamespace(to_artifact_id="src", pinned_n=read_n, at="2026-09-27T18:02:11Z")]
    store = FakeClient({("dep", mine_n): mine, ("src", read_n): theirs})
    session = SimpleNamespace(
        artifact_mgr=FakeManager(
            summaries, edges, {("src", read_n): "Line one\nLine two\nLine three"}
        ),
        artifacts=SimpleNamespace(client=store),
    )
    ctl = ArtifactController(session)
    ctl._store = store  # so a test can see which versions were asked for
    # Signed in. Every method here now asks `_unavailable()` first, and
    # a session that cannot reach the server is refused before it reads
    # anything — which is the point of that guard, but it is not what
    # these tests are about. Without this the refusal happens first and
    # the failure reads as "no versions were asked for", several layers
    # from the cause.
    ctl._collaboration = lambda: SimpleNamespace(is_configured=True)
    return ctl


class TestItComparesSomethingRealOnBothSides:
    @pytest.mark.asyncio
    async def test_it_reads_this_document_at_its_head_not_at_zero(self):
        # The bug. ``passages(n=0)`` matches no version, so the left
        # side was empty and every candidate came back with "nothing
        # here resembles it" — a confident false negative.
        ctl = controller(
            mine=[passage("a", "retry advice", 1, SAME)],
            theirs=[passage("b", "rate limits", 18, ALSO_SAME)],
            mine_n=3,
        )

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert ("dep", 3) in ctl._store.asked
        assert ("dep", 0) not in ctl._store.asked
        assert result.total_passages == 1

    @pytest.mark.asyncio
    async def test_it_reads_the_source_at_the_version_that_was_read(self):
        # Not at the source's head. The decision is about what the
        # agent actually had in front of it.
        ctl = controller(
            mine=[passage("a", "x", 1, SAME)],
            theirs=[passage("b", "y", 18, SAME)],
            read_n=5,
        )

        await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert ("src", 5) in ctl._store.asked
        assert ("src", 9) not in ctl._store.asked

    @pytest.mark.asyncio
    async def test_nothing_to_compare_is_reported_as_an_error_not_as_no_overlap(self):
        ctl = controller(mine=[], theirs=[passage("b", "y", 1, SAME)])

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert result.overlaps == []
        # The load-bearing assertion: silence here would read as
        # "measured, and found nothing in common".
        assert result.error

    @pytest.mark.asyncio
    async def test_a_source_with_no_indexed_passages_says_which_side_is_missing(self):
        ctl = controller(mine=[passage("a", "x", 1, SAME)], theirs=[], read_n=5)

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert "v5" in result.error
        assert "Login flow" in result.error


class TestWhatItReports:
    @pytest.mark.asyncio
    async def test_a_real_overlap_is_found_and_scored(self):
        ctl = controller(
            mine=[passage("a", "honour Retry-After", 1, SAME)],
            theirs=[passage("b", "Retry-After is seconds", 18, ALSO_SAME)],
        )

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert len(result.overlaps) == 1
        assert result.overlaps[0].similarity >= FLOOR
        assert result.overlaps[0].strength
        assert result.passages_touched == 1

    @pytest.mark.asyncio
    async def test_no_overlap_is_an_empty_list_and_no_error(self):
        # Measured, and found nothing — which is the answer a reviewer
        # most needs, and is distinguishable from the case above.
        ctl = controller(
            mine=[passage("a", "retry advice", 1, SAME)],
            theirs=[passage("b", "session cookies", 18, UNRELATED)],
        )

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert result.overlaps == []
        assert result.error == ""
        assert result.total_passages == 1
        assert result.passages_touched == 0

    @pytest.mark.asyncio
    async def test_it_counts_how_much_of_the_document_is_involved(self):
        # "2 of 14" and "11 of 14" are different answers about whether
        # this document was built on that one.
        ctl = controller(
            mine=[
                passage("a", "one", 1, SAME),
                passage("b", "two", 5, SAME),
                passage("c", "three", 9, UNRELATED),
            ],
            theirs=[passage("s", "source", 18, ALSO_SAME)],
        )

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert result.passages_touched == 2
        assert result.total_passages == 3

    @pytest.mark.asyncio
    async def test_the_strongest_overlap_comes_first(self):
        ctl = controller(
            mine=[
                passage("weak", "loosely related", 1, ALSO_SAME),
                passage("strong", "nearly identical", 5, SAME),
            ],
            theirs=[passage("s", "source", 18, SAME)],
        )

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        scores = [o.similarity for o in result.overlaps]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_the_quoted_source_text_is_whole_lines(self):
        # Same repair the cards get: a chunk is cut to a character
        # budget, so quoting it raw opens mid-sentence.
        ctl = controller(
            mine=[passage("a", "x", 1, SAME)],
            theirs=[passage("b", "ne two\nLine thr", 2, SAME)],
        )

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert result.overlaps[0].there_text.startswith("Line two")

    @pytest.mark.asyncio
    async def test_it_carries_when_the_read_was_captured(self):
        ctl = controller(mine=[passage("a", "x", 1, SAME)], theirs=[passage("b", "y", 1, SAME)])

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert result.captured_at == "2026-09-27T18:02:11Z"
        assert result.source_name == "Login flow"


class TestWhenItCannotAnswer:
    @pytest.mark.asyncio
    async def test_artifacts_disabled(self):
        ctl = ArtifactController(SimpleNamespace(artifacts=None, artifact_mgr=None))
        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")
        assert "disabled" in result.error.lower()

    @pytest.mark.asyncio
    async def test_a_document_that_is_not_there(self):
        ctl = controller(mine=[], theirs=[])
        result = await ctl.candidate_evidence(artifact_id="nope", source_id="src")
        assert result.error
        assert result.overlaps == []


class TestItIsWorkedOutOnceAndKept:
    """Recomputing costs twice for one answer, and can change it.

    The overlap is every passage against every passage; the verdict is
    a model call. Neither side moves on its own, so a stored answer
    about the same pair of versions is still the answer — and a
    *verdict* recomputed can come back different, which means a
    reviewer who reopens a row to think about a recommendation finds
    it changed under them.
    """

    @pytest.mark.asyncio
    async def test_the_second_ask_does_not_recompute(self):
        ctl = controller(
            mine=[passage("a", "x", 1, SAME)],
            theirs=[passage("b", "y", 18, ALSO_SAME)],
        )

        first = await ctl.candidate_evidence(artifact_id="dep", source_id="src")
        ctl._store.asked.clear()
        second = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert [o.similarity for o in second.overlaps] == [o.similarity for o in first.overlaps]
        assert second.passages_touched == first.passages_touched
        # The passages were re-read to find the edge, but the matching
        # did not run again — the stored overlap was served.
        assert ctl._store.writes == 1

    @pytest.mark.asyncio
    async def test_a_stored_answer_about_other_versions_is_not_served(self):
        # The cache key is the version pair, and it has to be: a
        # review of v3 against v5 says nothing about v4 against v6,
        # and a stale verdict reads exactly like a fresh one.
        ctl = controller(
            mine=[passage("a", "x", 1, SAME)],
            theirs=[passage("b", "y", 18, ALSO_SAME)],
        )
        await ctl.candidate_evidence(artifact_id="dep", source_id="src")
        ctl._store.stored["review_dependent_n"] = 99

        await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert ctl._store.writes == 2

    @pytest.mark.asyncio
    async def test_a_stored_answer_about_another_source_version_is_not_served(self):
        ctl = controller(
            mine=[passage("a", "x", 1, SAME)],
            theirs=[passage("b", "y", 18, ALSO_SAME)],
        )
        await ctl.candidate_evidence(artifact_id="dep", source_id="src")
        ctl._store.stored["review_source_n"] = 77

        await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert ctl._store.writes == 2

    @pytest.mark.asyncio
    async def test_an_unreadable_stored_answer_is_replaced_not_raised(self):
        # Losing the cache costs time, not truth.
        ctl = controller(
            mine=[passage("a", "x", 1, SAME)],
            theirs=[passage("b", "y", 18, ALSO_SAME)],
        )
        await ctl.candidate_evidence(artifact_id="dep", source_id="src")
        ctl._store.stored["review_overlaps"] = "{not json"

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert len(result.overlaps) == 1
        assert ctl._store.writes == 2

    @pytest.mark.asyncio
    async def test_a_store_that_refuses_to_keep_it_is_not_an_error(self):
        ctl = controller(
            mine=[passage("a", "x", 1, SAME)],
            theirs=[passage("b", "y", 18, ALSO_SAME)],
        )

        async def refuse(**_):
            raise RuntimeError("read-only")

        ctl._store.set_edge_review = refuse

        result = await ctl.candidate_evidence(artifact_id="dep", source_id="src")

        assert result.error == ""
        assert len(result.overlaps) == 1
