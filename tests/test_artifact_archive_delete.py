"""Ending a document, and the two ways of doing it.

Deleting takes the whole version chain with it, so every
``DERIVED_FROM`` edge pointing into any version of it breaks. The
damage does not show up on the screen where the delete was pressed:
it shows up later, on a different document, as a source that has
quietly stopped saying what it was. Nobody connects the two.

So a confirmed dependent refuses the delete, and archiving is the
thing offered instead — out of the tree, out of the list, every
version and every edge still resolving.

Candidates deliberately do not refuse it. A candidate is the agent's
guess that a read happened and nobody has ruled on it; a guess that
can veto a person's delete is a guess people learn to route around,
and they stop reading the confirmed ones too.
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

    async def dependents_of(self, artifact_id: str) -> list[dict]:
        if self._raises:
            raise RuntimeError("bolt is down")
        return list(self._dependents)

    async def set_archived(self, *, artifact_id: str, archived: bool) -> int | None:
        if artifact_id not in self.known:
            return None
        self.archived.append((artifact_id, archived))
        return self.children if archived else 0


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
    return ArtifactController(session), mgr


def dependent(name: str, state: str, artifact_id: str = "dep-1") -> dict:
    return {"artifact_id": artifact_id, "name": name, "state": state, "archived": False}


class TestDeleteIsRefusedForSomethingBuiltOnIt:
    @pytest.mark.asyncio
    async def test_a_confirmed_dependent_stops_it(self):
        store = FakeStore([dependent("Client retry guide", "confirmed")])
        ctl, mgr = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert mgr.deleted == []
        assert not result.ok

    @pytest.mark.asyncio
    async def test_it_names_what_is_in_the_way(self):
        # "3 documents depend on this" tells a reader they are stuck.
        # Naming them tells them what to go and change.
        store = FakeStore(
            [
                dependent("Client retry guide", "confirmed", "a"),
                dependent("Release checklist", "confirmed", "b"),
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
    async def test_it_offers_the_thing_that_would_work(self):
        store = FakeStore([dependent("Client retry guide", "confirmed")])
        ctl, _ = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert result.suggest_archive

    @pytest.mark.asyncio
    async def test_the_sentence_agrees_with_itself_for_one(self):
        store = FakeStore([dependent("Client retry guide", "confirmed")])
        ctl, _ = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert " is built on this" in result.error
        assert "that document" in result.error


class TestWhatDoesNotStopIt:
    @pytest.mark.asyncio
    async def test_a_candidate_does_not_veto_a_person(self):
        # Nobody has ruled on it. A guess that can refuse a delete is
        # a guess people learn to route around.
        store = FakeStore([dependent("Client retry guide", "candidate")])
        ctl, mgr = controller(store)

        result = await ctl.delete_node(node_id="doc-1")

        assert result.ok
        assert mgr.deleted == ["doc-1"]

    @pytest.mark.asyncio
    async def test_a_dismissed_edge_does_not(self):
        store = FakeStore([dependent("Client retry guide", "dismissed")])
        ctl, mgr = controller(store)

        assert (await ctl.delete_node(node_id="doc-1")).ok
        assert mgr.deleted == ["doc-1"]

    @pytest.mark.asyncio
    async def test_nothing_depending_on_it_deletes(self):
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
