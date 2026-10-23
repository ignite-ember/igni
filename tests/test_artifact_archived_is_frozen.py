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

**Frozen means the content, and nothing else.** An archived document
is still a document other work can be built on, and still something a
dependent can stop depending on. Both matter more than they sound:
archiving is offered *instead of* deleting precisely because the
lineage needs the thing to go on existing, and a document nobody can
cite or stop citing is a dead end wearing an archive's name. The
second class below exists because that half is invisible — nothing
fails, edges simply never form, and the first person to notice is
whoever wonders why a source list is short.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.models import (
    ArtifactSummary,
    SourceDeclaration,
    VersionPin,
)
from ember_code.core.code_index.embedder import HashEmbedder
from evals.archivist.store import FakeSharedServer


class FakeClient:
    """Enough of the store to resolve a name and notice a write."""

    def __init__(self, summary: ArtifactSummary | None) -> None:
        self._summary = summary
        self.writes = 0

    async def get_artifact(self, artifact_id: str) -> ArtifactSummary | None:
        return self._summary

    async def find_by_name(self, name: str) -> ArtifactSummary | None:
        return self._summary


def _signed_in(index: ArtifactIndex) -> ArtifactIndex:
    """Artifacts are a cloud service, so an unattached index refuses
    every write — which would pass these tests for the wrong reason."""
    index.attach_shared(FakeSharedServer())
    return index


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


class RecordingClient:
    """A store that answers, and remembers what it was asked to write.

    Separate from :class:`FakeClient` above, which stubs the write out
    entirely so the refusal tests can prove nothing reached it. These
    two need the opposite: the write has to happen so its edges can be
    inspected.
    """

    def __init__(self, *, archived: dict[str, bool]) -> None:
        self._archived = archived
        self.writes: list[dict] = []

    async def get_artifact(self, artifact_id: str) -> ArtifactSummary | None:
        if artifact_id not in self._archived:
            return None
        return ArtifactSummary(
            artifact_id=artifact_id,
            name=artifact_id,
            head_n=1,
            archived=self._archived[artifact_id],
        )

    async def find_by_name(self, name: str) -> ArtifactSummary | None:
        return await self.get_artifact(name)

    async def append_version(self, **kwargs) -> Any:
        self.writes.append(kwargs)
        return SimpleNamespace(
            n=2,
            artifact_id=kwargs["artifact_id"],
            version_id="v2",
            edges_written=len(kwargs.get("pins") or []),
            unresolved_pins=[],
        )

    async def record_candidates(self, **_) -> int:
        return 0


def recording_index(**archived: bool) -> tuple[ArtifactIndex, RecordingClient]:
    client = RecordingClient(archived=archived)
    return (
        _signed_in(ArtifactIndex(project="/tmp/p", neo4j_client=client, embedder=HashEmbedder())),
        client,
    )


class TestFrozenIsOnlyTheContent:
    """What an archived document can still take part in."""

    @pytest.mark.asyncio
    async def test_a_new_document_can_be_built_on_an_archived_one(self):
        # The reason archiving is offered instead of deleting. A
        # source that cannot be cited any more is deleted in every
        # sense that matters to the person citing it.
        idx, client = recording_index(frozen=True)

        result = await idx.create(
            name="Client retry guide",
            content="Written against the limits the archived document set.",
            sources=SourceDeclaration(pins=[VersionPin(artifact_id="frozen", n=1)]),
        )

        assert result.success, result.error
        assert [p.artifact_id for p in client.writes[0]["pins"]] == ["frozen"]

    @pytest.mark.asyncio
    async def test_a_dependent_can_stop_depending_on_an_archived_one(self):
        # The other half, and the one that is invisible when it
        # breaks: nothing fails, the edge simply stays, and the first
        # person to notice is whoever wonders why a document still
        # claims a source it stopped using.
        idx, client = recording_index(guide=False, frozen=True)

        result = await idx.revise(
            ref="guide",
            content="No longer built on the archived one.",
            sources=SourceDeclaration(),
        )

        assert result.success, result.error
        assert client.writes[0]["pins"] == []

    @pytest.mark.asyncio
    async def test_the_archived_one_still_refuses_its_own_edit(self):
        # The line between the two. Everything above is allowed
        # *because* the content cannot move.
        idx, client = recording_index(frozen=True)

        result = await idx.revise(ref="frozen", content="Sneaky edit.", sources=SourceDeclaration())

        assert not result.success
        assert client.writes == []


class TestRulingOnACommentTheServerHasNeverSeen:
    """A 404 is not a network failure.

    ``NotAllowed`` carries the comment id and nothing else, so its
    ``str()`` is a bare id — which made the refusal read "Could not
    reach the server: local-cdcb975178854562" and sent people to
    check their connection over a comment that was never there.

    It is the ordinary state of a comment written before comments
    went to the shared copy, so it has to say what is actually true.
    """

    @pytest.mark.asyncio
    async def test_it_says_the_comment_is_not_on_the_shared_copy(self):
        from ember_code.core.artifacts.collaboration import NotAllowed
        from ember_code.core.artifacts.session_manager import SessionArtifactManager

        class Refusing:
            async def resolve(self, **_):
                raise NotAllowed("local-cdcb975178854562")

        mgr = SessionArtifactManager.__new__(SessionArtifactManager)
        mgr.artifacts = SimpleNamespace(client=SimpleNamespace())

        result = await mgr.resolve_comment(
            comment_id="local-cdcb975178854562",
            status="resolved",
            content="",
            sync=Refusing(),
        )

        assert not result.ok
        # The thing that was wrong: the id, presented as a reason.
        assert "local-cdcb975178854562" not in (result.message or "")
        assert "only exists on this machine" in (result.message or "")

    @pytest.mark.asyncio
    async def test_a_real_failure_still_reads_as_one(self):
        # The distinction is only worth drawing if the other branch
        # still says what it used to.
        from ember_code.core.artifacts.session_manager import SessionArtifactManager

        class Broken:
            async def resolve(self, **_):
                raise RuntimeError("connection refused")

        mgr = SessionArtifactManager.__new__(SessionArtifactManager)
        mgr.artifacts = SimpleNamespace(client=SimpleNamespace())

        result = await mgr.resolve_comment(
            comment_id="c1", status="resolved", content="", sync=Broken()
        )

        assert not result.ok
        assert "connection refused" in (result.message or "")
