"""An edit waits in the draft until somebody publishes it.

Three things can change a document here: a person typing in the
editor, the agent answering a comment that asked for a change, and
the tools. They used to make a version each, which had two costs. An
afternoon's typing became forty entries in a list people read to find
out what changed. And a change *asked for* in a comment was published
before the person who asked had read it — they came back to a version
they had not agreed to, made in their name.

So the edit goes to the draft, and publishing is a separate act.

The part worth testing is that it works for **documents nobody has
shared**, which is most of them. A shared document keeps its draft on
the server, where everybody who can read the document can watch it
being written; a local one has nobody to watch, and still needs
somewhere other than an editor to keep an unfinished sentence. The
same two acts, with a file where the server would be — and the same
rule about which one makes a version.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.artifacts.draft_desk import DraftDesk, DraftRefused
from ember_code.core.artifacts.drafts import LocalDrafts
from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import SourceDeclaration
from ember_code.core.code_index.embedder import HashEmbedder
from evals.archivist.store import FakeArtifactStore

CODEC = ArtifactMetadataCodec()


@pytest.fixture
async def index(tmp_path: Path) -> ArtifactIndex:
    index = ArtifactIndex(
        project=tmp_path / "project",
        data_dir=tmp_path / "igni",
        neo4j_client=FakeArtifactStore(),  # type: ignore[arg-type]
        embedder=HashEmbedder(),
    )
    await index.create(
        name="Auth model",
        content="Auth is a session cookie.",
        sources=SourceDeclaration(),
        kind="design",
    )
    index.ledger.clear()
    return index


@pytest.fixture
def desk(index) -> DraftDesk:
    # No server attached, which is what "never shared" looks like from
    # here — and the state most documents are in.
    return DraftDesk(index, author="maya@acme.test")


@pytest.fixture
def doc_id() -> str:
    return CODEC.artifact_id("Auth model")


class TestWritingOne:
    async def test_saving_makes_no_version(self, desk, index, doc_id):
        # The whole change, in one assertion.
        await desk.save(doc_id, "Auth is a signed JWT.")

        found = await index.resolve(doc_id)
        assert found is not None
        assert found.head_n == 1

    async def test_the_document_still_says_the_old_thing(self, desk, index, doc_id):
        await desk.save(doc_id, "Auth is a signed JWT.")

        head = await index.version(doc_id, 0)
        assert head is not None
        assert "session cookie" in head.content

    async def test_it_comes_back(self, desk, doc_id):
        await desk.save(doc_id, "Half a thought.")

        draft = await desk.read(doc_id)

        assert draft.exists is True
        assert draft.content == "Half a thought."

    async def test_saving_again_replaces_it(self, desk, doc_id):
        # One draft per document, not a pile of them.
        await desk.save(doc_id, "First.")
        await desk.save(doc_id, "Second.")

        assert (await desk.read(doc_id)).content == "Second."

    async def test_a_document_nobody_has_edited_has_no_draft(self, desk, doc_id):
        draft = await desk.read(doc_id)

        assert draft.exists is False
        assert draft.head_n == 1

    async def test_an_emptied_draft_is_not_the_same_as_no_draft(self, desk, doc_id):
        # Somebody deleting every word is a decision about what the
        # document should say. Nobody having started is not.
        await desk.save(doc_id, "")

        draft = await desk.read(doc_id)
        assert draft.exists is True
        assert draft.content == ""

    async def test_it_records_which_version_it_was_written_from(self, desk, doc_id):
        await desk.save(doc_id, "From v1.")

        assert (await desk.read(doc_id)).base_n == 1

    async def test_it_outlives_the_process(self, index, desk, doc_id, tmp_path):
        # The point of a draft rather than an open editor: the words
        # are somewhere, and somewhere is not this tab.
        await desk.save(doc_id, "Left half-written.")

        again = DraftDesk(index)
        assert (await again.read(doc_id)).content == "Left half-written."


class TestPublishingOne:
    async def test_it_becomes_a_version(self, desk, doc_id):
        await desk.save(doc_id, "Auth is a signed JWT.")

        n = await desk.publish(doc_id)

        assert n == 2

    async def test_the_version_says_what_the_draft_said(self, desk, index, doc_id):
        await desk.save(doc_id, "Auth is a signed JWT.")
        await desk.publish(doc_id)

        head = await index.version(doc_id, 0)
        assert head is not None
        assert "signed JWT" in head.content

    async def test_the_old_version_is_still_readable(self, desk, index, doc_id):
        await desk.save(doc_id, "Auth is a signed JWT.")
        await desk.publish(doc_id)

        old = await index.version(doc_id, 1)
        assert old is not None
        assert "session cookie" in old.content

    async def test_publishing_clears_the_draft(self, desk, doc_id):
        # Leaving it would show the next person an "unsaved change"
        # word for word identical to the version they are reading.
        await desk.save(doc_id, "Done with it.")
        await desk.publish(doc_id)

        assert (await desk.read(doc_id)).exists is False

    async def test_there_is_nothing_to_publish_without_a_draft(self, desk, doc_id):
        with pytest.raises(DraftRefused):
            await desk.publish(doc_id)

    async def test_a_refused_append_keeps_the_draft(self, desk, index, doc_id):
        # The draft is the only copy of what was written, and the
        # refusal — an archived document, usually — is a thing
        # somebody can undo and then want their words back.
        await desk.save(doc_id, "Worth keeping.")

        async def refuse(**_kwargs):
            class Refused:
                success = False
                error = "Archived."
                n = 0

            return Refused()

        index.revise = refuse  # type: ignore[method-assign]
        with pytest.raises(DraftRefused):
            await desk.publish(doc_id)

        assert (await desk.read(doc_id)).content == "Worth keeping."

    async def test_the_version_is_attributed_to_whoever_published_it(
        self, desk, index, doc_id
    ):
        await desk.save(doc_id, "Mine.")
        await desk.publish(doc_id)

        head = await index.version(doc_id, 0)
        assert head is not None
        assert head.author == "maya@acme.test"


class TestOneDraftPerDocument:
    async def test_two_documents_do_not_share_one(self, desk, index):
        await index.create(
            name="Session handling",
            content="Something else entirely.",
            sources=SourceDeclaration(),
            kind="design",
        )
        first, second = CODEC.artifact_id("Auth model"), CODEC.artifact_id("Session handling")

        await desk.save(first, "About auth.")
        await desk.save(second, "About sessions.")

        assert (await desk.read(first)).content == "About auth."
        assert (await desk.read(second)).content == "About sessions."

    async def test_two_projects_do_not_share_one(self, tmp_path):
        # Ids are derived from names, so two projects can hold
        # artifacts with the same id — and one project's draft
        # appearing in another is somebody else's sentence in your
        # document.
        here = LocalDrafts(data_dir=tmp_path, project_id="one")
        there = LocalDrafts(data_dir=tmp_path, project_id="two")

        here.write(artifact_id="a1", content="Ours.", base_n=1)

        assert there.read(artifact_id="a1") is None
        assert here.read(artifact_id="a1") is not None


class TestTheFileItself:
    def test_a_name_cannot_write_outside_the_directory(self, tmp_path):
        # Ids are minted here and are safe, but this takes whatever it
        # is handed and a path is a poor place to find that out.
        drafts = LocalDrafts(data_dir=tmp_path, project_id="p")

        drafts.write(artifact_id="../../escaped", content="Nope.", base_n=0)

        written = list(tmp_path.rglob("*escaped*"))
        assert written, "the draft was not written at all"
        assert all(f.parent == tmp_path / "drafts" / "p" for f in written)
        assert drafts.read(artifact_id="../../escaped") is not None

    def test_an_unreadable_draft_reads_as_none(self, tmp_path):
        # The document is intact in the store, and a draft this
        # process cannot parse must not stand between somebody and the
        # document they asked for.
        drafts = LocalDrafts(data_dir=tmp_path, project_id="p")
        drafts.write(artifact_id="a1", content="Fine.", base_n=1)
        broken = next((tmp_path / "drafts" / "p").glob("*.json"))
        broken.write_text("{not json", encoding="utf-8")

        assert drafts.read(artifact_id="a1") is None

    def test_nothing_is_left_behind_after_publishing(self, tmp_path):
        drafts = LocalDrafts(data_dir=tmp_path, project_id="p")
        drafts.write(artifact_id="a1", content="Gone soon.", base_n=1)

        drafts.clear(artifact_id="a1")

        assert list((tmp_path / "drafts" / "p").glob("*.json")) == []

    def test_clearing_one_that_was_never_written_is_not_an_error(self, tmp_path):
        LocalDrafts(data_dir=tmp_path, project_id="p").clear(artifact_id="never")
