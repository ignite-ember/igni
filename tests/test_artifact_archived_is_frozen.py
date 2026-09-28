"""An archived document does not change.

Archiving is what is offered instead of deleting something the lineage
depends on. That offer is only worth anything if the archived thing
stops moving: a document whose dependents were told "this is what it
was built on" must not quietly gain a v7 afterwards, because then the
archive is just a place where edits happen unwatched.

The gate is on :meth:`ArtifactIndex.revise` rather than on each
caller. Accepting a comment, applying a drift fix and the agent's own
revise tool all arrive there, and a gate per entrance is a gate
somebody adds an entrance around.
"""

from __future__ import annotations

import pytest

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.models import ArtifactSummary


class FakeClient:
    """Enough of the store to resolve a name and notice a write."""

    def __init__(self, summary: ArtifactSummary | None) -> None:
        self._summary = summary
        self.writes = 0

    async def get_artifact(self, artifact_id: str) -> ArtifactSummary | None:
        return self._summary

    async def find_by_name(self, name: str) -> ArtifactSummary | None:
        return self._summary


def index_for(summary: ArtifactSummary | None) -> tuple[ArtifactIndex, FakeClient]:
    client = FakeClient(summary)
    idx = ArtifactIndex(project="/tmp/p", neo4j_client=client)

    async def _explode(**_):
        raise AssertionError("a frozen document must never reach the write")

    idx._write = _explode  # type: ignore[method-assign]
    return idx, client


def summary(*, archived: bool, name: str = "Rate limiting") -> ArtifactSummary:
    return ArtifactSummary(artifact_id="a1", name=name, head_n=3, archived=archived)


class TestReviseIsRefused:
    @pytest.mark.asyncio
    async def test_an_archived_document_will_not_take_a_new_version(self):
        idx, _ = index_for(summary(archived=True))

        result = await idx.revise(ref="Rate limiting", content="new text", sources=[])

        assert not result.success
        assert "archived" in (result.error or "")

    @pytest.mark.asyncio
    async def test_the_refusal_says_how_to_undo_it(self):
        # A dead end teaches people the feature is broken. Naming
        # restore makes it a step.
        idx, _ = index_for(summary(archived=True))

        result = await idx.revise(ref="Rate limiting", content="new text", sources=[])

        assert "Restore it first" in (result.error or "")

    @pytest.mark.asyncio
    async def test_it_names_the_document_not_the_reference(self):
        idx, _ = index_for(summary(archived=True, name="Rate limiting"))

        result = await idx.revise(ref="a1", content="new text", sources=[])

        assert "Rate limiting" in (result.error or "")

    @pytest.mark.asyncio
    async def test_an_ordinary_document_still_writes(self):
        # The gate must not be the reason every revise stops working.
        idx, _ = index_for(summary(archived=False))

        with pytest.raises(AssertionError, match="must never reach"):
            await idx.revise(ref="Rate limiting", content="new text", sources=[])

    @pytest.mark.asyncio
    async def test_a_missing_document_is_still_the_missing_message(self):
        # Not "archived": the two are different problems and only one
        # of them has restore as the answer.
        idx, _ = index_for(None)

        result = await idx.revise(ref="nope", content="new text", sources=[])

        assert not result.success
        assert "No artifact matches" in (result.error or "")
