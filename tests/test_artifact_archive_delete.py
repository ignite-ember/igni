"""Ending a document, and the two ways of doing it.

**A document in the lineage graph is archived, never deleted.**
Deleting takes the whole version chain with it, and every
``DERIVED_FROM`` edge touching any version goes with it. The damage
does not show up on the screen where the delete was pressed: it shows
up later, on a different document, as a source that has quietly
stopped saying what it was. Nobody connects the two.

Both directions count, which is the part that is easy to get wrong.
A document built on this one obviously breaks. So does this
document's own *source*: the edge is the record of what this was
written from, and the source's list of dependents is how anybody
finds out the thing existed. Cutting either end destroys the same
fact.

Dismissed edges are the exception. "This was not a source" is a
ruling that there is no relationship, and holding a document hostage
to an edge somebody already rejected would make the dismissal
meaningless.

And archiving freezes. It is what is offered instead of deleting
something the lineage depends on, so it is only worth offering if the
archived thing stops moving — a document whose dependents were told
"this is what it was built on" must not quietly gain a v7 afterwards.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.backend.server_artifacts import ArtifactController


class FakeStore:
    def __init__(
        self,
        dependents: list[dict] | None = None,
        *,
        raises: bool = False,
        children: int = 0,
    ) -> None:
        self._dependents = dependents or []
        self._raises = raises
        self.children = children
        self.archived: list[tuple[str, bool]] = []
        self.known = {"doc-1"}

    async def lineage_ties(self, artifact_id: str) -> list[dict]:
        if self._raises:
            raise RuntimeError("bolt is down")
        return list(self._dependents)

    async def set_archived(self, *, artifact_id: str, archived: bool) -> int | None:
        if artifact_id not in self.known:
            return None
        self.archived.append((artifact_id, archived))
        return self.children if archived else 0

    async def get_artifact(self, artifact_id: str):
        """Is this id a document, or a folder?

        Asked before archiving, to decide which endpoint files the
        shared copy. Answering for the ids this store knows keeps the
        two questions consistent — a document that can be archived is
        a document that exists.
        """
        if artifact_id not in self.known:
            return None
        return SimpleNamespace(artifact_id=artifact_id, name="A document")


class FakeManager:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, entry_id: str) -> bool:
        self.deleted.append(entry_id)
        return True


def controller(store: FakeStore) -> tuple[ArtifactController, FakeManager]:
    mgr = FakeManager()
    session = SimpleNamespace(
        artifact_mgr=mgr,
        artifacts=SimpleNamespace(client=store),
    )
    ctl = ArtifactController(session)

    # Signed in, with nothing published yet.
    #
    # Two things need the sync and they pull in opposite directions:
    # `_unavailable()` refuses a session that cannot reach the server,
    # so it must look configured; and `_shared_place` files the shared
    # copy before archiving, which needs a slug. `slug_for` answering
    # None is the "never published" case — the shared copy has nothing
    # to file, which is true of every document in this file and is the
    # arrangement these tests mean.
    async def _never_published(_node_id: str) -> str | None:
        return None

    ctl._collaboration = lambda: SimpleNamespace(is_configured=True, slug_for=_never_published)
    return ctl, mgr


def tie(
    name: str,
    state: str,
    artifact_id: str = "dep-1",
    direction: str = "dependent",
) -> dict:
    return {
        "artifact_id": artifact_id,
        "name": name,
        "state": state,
        "archived": False,
        "direction": direction,
    }


class TestLineageMeansArchiveOnly:
    @pytest.mark.asyncio
    async def test_a_document_built_on_this_one_stops_it(self):
        store = FakeStore([tie("Client retry guide", "confirmed")])
        ctl, mgr = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert mgr.deleted == []
        assert not result.ok

    @pytest.mark.asyncio
    async def test_its_own_source_stops_it_too(self):
        # The half that is easy to miss. The edge is the record of
        # what this was written from, and the source's dependents list
        # is how anybody finds out this document existed — deleting
        # from this end destroys the same fact.
        store = FakeStore([tie("Rate limiting", "confirmed", "src", direction="source")])
        ctl, mgr = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert mgr.deleted == []
        assert "it is built on Rate limiting" in result.error

    @pytest.mark.asyncio
    async def test_a_candidate_stops_it(self):
        # An unreviewed read is still a link on the graph, and the
        # answer for anything on the graph is archive.
        store = FakeStore([tie("Client retry guide", "candidate")])
        ctl, mgr = controller(store)

        assert not (await ctl.delete_node(node_id="doc-1")).ok
        assert mgr.deleted == []

    @pytest.mark.asyncio
    async def test_it_names_what_is_in_the_way(self):
        # "2 documents" tells a reader they are stuck. Naming them
        # tells them what to go and change.
        store = FakeStore(
            [
                tie("Client retry guide", "confirmed", "a"),
                tie("Release checklist", "confirmed", "b"),
            ]
        )
        ctl, _ = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert [d.name for d in result.blocked_by] == [
            "Client retry guide",
            "Release checklist",
        ]
        assert "Client retry guide" in result.error
        assert "Release checklist" in result.error

    @pytest.mark.asyncio
    async def test_which_way_each_link_runs_travels_with_it(self):
        # Flattened to "related", the reader cannot tell whether to go
        # and edit the other document or this one.
        store = FakeStore(
            [
                tie("Client retry guide", "confirmed", "a"),
                tie("Rate limiting", "confirmed", "b", direction="source"),
            ]
        )
        ctl, _ = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert {d.name: d.direction for d in result.blocked_by} == {
            "Client retry guide": "dependent",
            "Rate limiting": "source",
        }

    @pytest.mark.asyncio
    async def test_both_directions_are_said_separately(self):
        store = FakeStore(
            [
                tie("Client retry guide", "confirmed", "a"),
                tie("Rate limiting", "confirmed", "b", direction="source"),
            ]
        )
        ctl, _ = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert "Client retry guide is built on it" in result.error
        assert "it is built on Rate limiting" in result.error

    @pytest.mark.asyncio
    async def test_it_offers_the_thing_that_would_work(self):
        store = FakeStore([tie("Client retry guide", "confirmed")])
        ctl, _ = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert result.suggest_archive
        assert "Archive it instead" in result.error

    @pytest.mark.asyncio
    async def test_the_sentence_agrees_with_itself(self):
        store = FakeStore(
            [
                tie("Client retry guide", "confirmed", "a"),
                tie("Release checklist", "confirmed", "b"),
            ]
        )
        ctl, _ = controller(store)

        assert "are built on it" in (await ctl.delete_node(node_id="doc-1")).error


class TestWhatDoesNotStopIt:
    @pytest.mark.asyncio
    async def test_a_dismissed_edge_does_not(self):
        # "This was not a source" is a ruling that there is no
        # relationship. Holding the document hostage to an edge
        # somebody already rejected would make the dismissal
        # meaningless.
        store = FakeStore([tie("Client retry guide", "dismissed")])
        ctl, mgr = controller(store)

        assert (await ctl.delete_node(node_id="doc-1")).ok
        assert mgr.deleted == ["doc-1"]

    @pytest.mark.asyncio
    async def test_a_document_on_no_edges_deletes(self):
        ctl, mgr = controller(FakeStore([]))

        result = await ctl.delete_node(node_id="doc-1")

        assert result.ok
        assert mgr.deleted == ["doc-1"]


class TestWhenTheCheckCannotBeMade:
    @pytest.mark.asyncio
    async def test_it_does_not_delete_anyway(self):
        # The one case where doing nothing is clearly right: an
        # unanswered question about what breaks is not a "no".
        store = FakeStore(raises=True)
        ctl, mgr = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert mgr.deleted == []
        assert not result.ok
        assert "nothing was deleted" in result.error


class TestArchiving:
    @pytest.mark.asyncio
    async def test_it_does_not_delete_anything(self):
        store = FakeStore()
        ctl, mgr = controller(store)

        result = await ctl.archive_node(node_id="doc-1")

        assert result.ok
        assert store.archived == [("doc-1", True)]
        assert mgr.deleted == []

    @pytest.mark.asyncio
    async def test_it_says_that_dependents_survive(self):
        # The reason to reach for archive over delete has to be on the
        # screen where the choice is made, not only in a refusal that
        # may never appear.
        ctl, _ = controller(FakeStore())

        result = await ctl.archive_node(node_id="doc-1")

        assert "still resolves" in result.message

    @pytest.mark.asyncio
    async def test_it_goes_both_ways(self):
        store = FakeStore()
        ctl, _ = controller(store)

        result = await ctl.archive_node(node_id="doc-1", archived=False)

        assert store.archived == [("doc-1", False)]
        assert result.message == "Restored."

    @pytest.mark.asyncio
    async def test_it_says_what_moved_out_from_under_it(self):
        # Anything under an archived document moves up a level, and
        # things that move without being asked to are exactly what
        # people discover a week later and cannot account for.
        ctl, _ = controller(FakeStore(children=2))

        result = await ctl.archive_node(node_id="doc-1")

        assert result.promoted == 2
        assert "2 items moved up a level" in result.message

    @pytest.mark.asyncio
    async def test_nothing_under_it_says_nothing(self):
        ctl, _ = controller(FakeStore(children=0))

        assert "moved up" not in (await ctl.archive_node(node_id="doc-1")).message

    @pytest.mark.asyncio
    async def test_archiving_something_absent_is_an_error(self):
        # Not a silent success: the reader would believe it was put
        # away and go looking for it in the archive.
        store = FakeStore()
        ctl, _ = controller(store)

        result = await ctl.archive_node(node_id="not-here")

        assert not result.ok
        assert result.error
