"""Where a document sits is written to the shared copy too.

Filing is the one part of a document's state nobody can work out from
its content. A version can be re-read, edges can be rebuilt from the
sources a version carries, embeddings can be recomputed — but nothing
in a document says it was dragged into "Platform". So a tree that
lived only in the local graph was a tree that existed for one person
on one machine, however shared the documents in it were.

Two things here are easy to get wrong and quiet when they are:

*The ids have to be the server's.* Both sides can mint a folder id,
and if they each mint their own they agree the folder exists and
agree about nothing else — every later rename, move or delete
addresses whichever copy the caller happened to be looking at, and
neither is wrong enough to raise.

*A refusal has to stop the local change.* Moving the graph to a shape
the shared copy rejected is the same split store as a local-only
version, arriving through a different door.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ember_code.backend.server_artifacts import ArtifactController
from ember_code.core.artifacts.collaboration import NotAllowed
from ember_code.core.artifacts.models import ArtifactSummary


class FakeGraph:
    """The local mirror, recording what it was told to do."""

    def __init__(
        self,
        documents: set[str] | None = None,
        folders: list[dict[str, Any]] | None = None,
    ) -> None:
        self.documents = documents or set()
        #: The local tree's folders, as ``folders()`` answers them.
        self.folders_here = folders or []
        self.created: list[dict[str, Any]] = []
        self.renamed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.archived: list[tuple[str, bool]] = []
        self.moved: list[tuple[str, str | None]] = []

    async def get_artifact(self, artifact_id: str) -> ArtifactSummary | None:
        if artifact_id in self.documents:
            return ArtifactSummary(artifact_id=artifact_id, name="Session handling", head_n=1)
        return None

    async def create_folder(self, *, name: str, parent_id: str = "", folder_id: str = "") -> str:
        chosen = folder_id or "local-mint"
        self.created.append({"name": name, "parent_id": parent_id, "folder_id": chosen})
        return chosen

    async def rename_folder(self, *, folder_id: str, name: str) -> bool:
        self.renamed.append((folder_id, name))
        return True

    async def delete_folder(self, *, folder_id: str) -> int:
        self.deleted.append(folder_id)
        return 0

    async def set_archived(self, *, artifact_id: str, archived: bool) -> int | None:
        self.archived.append((artifact_id, archived))
        return 0

    async def set_parent(self, *, node_id: str, parent_id: str | None) -> bool:
        self.moved.append((node_id, parent_id))
        return True

    async def list_artifacts(self, **_: Any) -> list[Any]:
        return []

    async def list_folders(self, **_: Any) -> list[Any]:
        return []

    async def folders(self) -> list[dict[str, Any]]:
        return list(self.folders_here)


class FakeServer:
    """The shared copy, recording what reached it."""

    is_configured = True

    def __init__(
        self,
        *,
        folder_id: str = "server-abc",
        fail: Exception | None = None,
        holds: set[str] | None = None,
        strict: bool = False,
    ) -> None:
        self._folder_id = folder_id
        self._fail = fail
        #: Folder ids this server has. ``strict`` makes filing into
        #: anything else answer the way the real one does.
        self.holds = holds if holds is not None else set()
        self._strict = strict
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.placed: list[dict[str, Any]] = []
        self.slugs = {"doc-1": "s1"}

    def _maybe_fail(self) -> None:
        if self._fail is not None:
            raise self._fail

    async def create_folder(self, *, name: str, parent_id: str = "", folder_id: str = "") -> str:
        self._maybe_fail()
        if self._strict and parent_id and parent_id not in self.holds:
            raise NotAllowed(parent_id)
        self.created.append({"name": name, "parent_id": parent_id, "folder_id": folder_id})
        chosen = folder_id or self._folder_id
        self.holds.add(chosen)
        return chosen

    async def update_folder(self, **kwargs: Any) -> None:
        self._maybe_fail()
        self.updated.append(kwargs)

    async def delete_folder(self, *, folder_id: str) -> None:
        self._maybe_fail()
        self.deleted.append(folder_id)

    async def place_document(self, **kwargs: Any) -> None:
        self._maybe_fail()
        parent = kwargs.get("parent_id") or ""
        if self._strict and parent and parent not in self.holds:
            # What the real server answers: it has no such folder, and
            # says so about the document it was asked to move.
            raise NotAllowed(parent)
        self.placed.append(kwargs)

    async def slug_for(self, artifact_id: str) -> str:
        return self.slugs.get(artifact_id, "")


class FakeManager:
    """Just enough of the session manager for the cycle check.

    ``move_node`` assembles the whole tree before it writes anything,
    to refuse a move that would close a loop. The documents come from
    here and the folders from the graph.
    """

    def __init__(self, graph: FakeGraph) -> None:
        self._graph = graph

    async def list_artifacts(self, **_: Any) -> list[ArtifactSummary]:
        return [
            ArtifactSummary(artifact_id=doc, name=doc, head_n=1)
            for doc in sorted(self._graph.documents)
        ]


def controller(graph: FakeGraph, server: FakeServer | None) -> ArtifactController:
    session = SimpleNamespace(
        artifact_mgr=FakeManager(graph), artifacts=SimpleNamespace(client=graph)
    )
    ctl = ArtifactController(session)
    # The controller builds its own client from session credentials,
    # which a test has none of. Replaced wholesale so the seam under
    # test is the routing, not the authentication.
    ctl._collaboration = lambda: server  # type: ignore[method-assign]
    return ctl


class TestAFolderIsNamedByTheServer:
    @pytest.mark.asyncio
    async def test_it_is_made_on_the_server(self):
        graph, server = FakeGraph(), FakeServer()

        await controller(graph, server).create_folder(name="Platform")

        assert server.created == [{"name": "Platform", "parent_id": "", "folder_id": ""}]

    @pytest.mark.asyncio
    async def test_and_the_graph_adopts_the_id_the_server_chose(self):
        # The whole reason the server goes first. Two copies that each
        # mint an id disagree about what a folder is called, and every
        # later operation addresses the wrong one.
        graph, server = FakeGraph(), FakeServer(folder_id="server-abc")

        result = await controller(graph, server).create_folder(name="Platform")

        assert graph.created[0]["folder_id"] == "server-abc"
        assert result.node_id == "server-abc"

    @pytest.mark.asyncio
    async def test_a_server_that_refuses_leaves_the_graph_alone(self):
        graph = FakeGraph()
        server = FakeServer(fail=RuntimeError("connection refused"))

        result = await controller(graph, server).create_folder(name="Platform")

        assert not result.ok
        assert graph.created == []


class TestRenamingAndRemoving:
    @pytest.mark.asyncio
    async def test_a_rename_goes_up_before_it_goes_in(self):
        graph, server = FakeGraph(), FakeServer()

        await controller(graph, server).rename_folder(folder_id="f1", name="Auth model")

        assert server.updated[0]["name"] == "Auth model"
        assert graph.renamed == [("f1", "Auth model")]

    @pytest.mark.asyncio
    async def test_a_refused_rename_does_not_happen_here(self):
        graph = FakeGraph()
        server = FakeServer(fail=NotAllowed("f1"))

        result = await controller(graph, server).rename_folder(folder_id="f1", name="Auth")

        assert not result.ok
        assert graph.renamed == []

    @pytest.mark.asyncio
    async def test_removing_a_folder_removes_the_shared_one_too(self):
        graph, server = FakeGraph(), FakeServer()

        await controller(graph, server).delete_folder(folder_id="f1")

        assert server.deleted == ["f1"]
        assert graph.deleted == ["f1"]

    @pytest.mark.asyncio
    async def test_a_refused_removal_keeps_the_folder_here(self):
        graph = FakeGraph()
        server = FakeServer(fail=RuntimeError("bolt is down"))

        result = await controller(graph, server).delete_folder(folder_id="f1")

        assert not result.ok
        assert graph.deleted == []


class TestTheTwoKindsOfNode:
    @pytest.mark.asyncio
    async def test_archiving_a_document_is_addressed_by_its_slug(self):
        graph, server = FakeGraph(documents={"doc-1"}), FakeServer()

        await controller(graph, server).archive_node(node_id="doc-1", archived=True)

        assert server.placed == [
            {"slug": "s1", "parent_id": None, "to_top": False, "archived": True}
        ]

    @pytest.mark.asyncio
    async def test_archiving_a_folder_is_addressed_by_its_id(self):
        # A folder has no slug, and its id is the same string on both
        # sides — which is why the server names it in the first place.
        graph, server = FakeGraph(), FakeServer()

        await controller(graph, server).archive_node(node_id="f1", archived=True)

        assert server.updated[0]["folder_id"] == "f1"
        assert server.placed == []

    @pytest.mark.asyncio
    async def test_a_document_that_was_never_published_files_locally(self):
        # Not an error. Nothing to file on a shared copy that does not
        # exist yet, and the next write gives it one.
        graph, server = FakeGraph(documents={"doc-2"}), FakeServer()

        result = await controller(graph, server).archive_node(node_id="doc-2", archived=True)

        assert result.ok
        assert server.placed == []
        assert graph.archived == [("doc-2", True)]


class TestWithNoAccount:
    @pytest.mark.asyncio
    async def test_filing_still_works_with_no_server(self):
        graph = FakeGraph()

        result = await controller(graph, None).create_folder(name="Platform")

        assert result.ok
        assert graph.created[0]["name"] == "Platform"


class TestAFolderTheServerHasNeverHeardOf:
    """Filing into a folder that was made while this machine was signed out.

    The reported symptom was simply "moving an article in the
    hierarchy does not update its position". Underneath: the folder
    existed here and nowhere else, filing asked the server to put a
    published document inside it, the server answered that there is no
    such folder — and the refusal stopped the local move as well. So
    the document did not move, and the reason given named a folder the
    person was looking straight at.

    The tree is not allowed to diverge, so the answer is not to skip
    the server. It is to give the server the folder.
    """

    @pytest.mark.asyncio
    async def test_the_folder_is_made_on_the_server_first(self):
        graph = FakeGraph(
            documents={"doc-1"},
            folders=[{"node_id": "f-local", "name": "Platform", "parent_id": ""}],
        )
        server = FakeServer(strict=True)

        result = await controller(graph, server).move_node(node_id="doc-1", parent_id="f-local")

        assert result.ok
        assert server.created == [{"name": "Platform", "parent_id": "", "folder_id": "f-local"}]
        assert server.placed == [
            {"slug": "s1", "parent_id": "f-local", "to_top": False, "archived": None}
        ]

    @pytest.mark.asyncio
    async def test_and_the_document_actually_moves_here(self):
        # The symptom, restated as an assertion.
        graph = FakeGraph(
            documents={"doc-1"},
            folders=[{"node_id": "f-local", "name": "Platform", "parent_id": ""}],
        )

        await controller(graph, FakeServer(strict=True)).move_node(
            node_id="doc-1", parent_id="f-local"
        )

        assert graph.moved == [("doc-1", "f-local")]

    @pytest.mark.asyncio
    async def test_the_whole_ancestry_is_made_outermost_first(self):
        # A folder cannot be created inside a parent the server has
        # not got, so the order is not an implementation detail.
        graph = FakeGraph(
            documents={"doc-1"},
            folders=[
                {"node_id": "inner", "name": "Auth", "parent_id": "outer"},
                {"node_id": "outer", "name": "Platform", "parent_id": ""},
            ],
        )
        server = FakeServer(strict=True)

        await controller(graph, server).move_node(node_id="doc-1", parent_id="inner")

        assert [f["folder_id"] for f in server.created] == ["outer", "inner"]
        assert server.created[1]["parent_id"] == "outer"

    @pytest.mark.asyncio
    async def test_a_folder_the_server_already_has_is_not_made_twice(self):
        graph = FakeGraph(
            documents={"doc-1"},
            folders=[{"node_id": "f-known", "name": "Platform", "parent_id": ""}],
        )
        # The server answers an existing folder of this owner's rather
        # than failing, so the call is harmless — and the one that
        # matters is the place.
        server = FakeServer(strict=True, holds={"f-known"}, folder_id="f-known")

        result = await controller(graph, server).move_node(node_id="doc-1", parent_id="f-known")

        assert result.ok
        assert server.placed[0]["parent_id"] == "f-known"

    @pytest.mark.asyncio
    async def test_moving_to_top_level_needs_no_folder_at_all(self):
        graph = FakeGraph(documents={"doc-1"})
        server = FakeServer(strict=True)

        result = await controller(graph, server).move_node(node_id="doc-1", parent_id="")

        assert result.ok
        assert server.created == []
        assert graph.moved == [("doc-1", None)]

    @pytest.mark.asyncio
    async def test_an_id_the_server_renames_is_reported_rather_than_half_done(self):
        # The id was taken by another account, so the server minted
        # its own and the two copies now disagree about what this
        # folder is. Filing would fail one call later and less
        # legibly; it is said here, where the message can name the
        # folder it is about.
        graph = FakeGraph(
            documents={"doc-1"},
            folders=[{"node_id": "f-local", "name": "Platform", "parent_id": ""}],
        )
        server = FakeServer(strict=True, folder_id="taken-elsewhere")

        async def rename_on_create(*, name, parent_id="", folder_id=""):
            server.created.append({"name": name, "parent_id": parent_id, "folder_id": folder_id})
            return "somebody-elses-id"

        server.create_folder = rename_on_create  # type: ignore[assignment]

        result = await controller(graph, server).move_node(node_id="doc-1", parent_id="f-local")

        assert not result.ok
        assert "Platform" in result.error
        assert graph.moved == []

    @pytest.mark.asyncio
    async def test_a_document_that_was_never_published_still_moves(self):
        # Nothing to file on the shared copy, so nothing to back-fill
        # either — and the local arrangement is what it has until the
        # next write gives it a home.
        graph = FakeGraph(
            documents={"doc-unpublished"},
            folders=[{"node_id": "f-local", "name": "Platform", "parent_id": ""}],
        )
        server = FakeServer(strict=True)
        server.slugs = {}

        result = await controller(graph, server).move_node(
            node_id="doc-unpublished", parent_id="f-local"
        )

        assert result.ok
        assert graph.moved == [("doc-unpublished", "f-local")]

    @pytest.mark.asyncio
    async def test_a_destination_this_machine_does_not_have_is_left_to_the_server(self):
        # The id names nothing local, so there is nothing to
        # back-fill. The server has the last word and refuses the
        # place itself, which is the honest answer.
        graph = FakeGraph(documents={"doc-1"})
        server = FakeServer(strict=True)

        result = await controller(graph, server).move_node(node_id="doc-1", parent_id="nowhere")

        assert not result.ok
        assert graph.moved == []
