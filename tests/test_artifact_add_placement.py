"""Where a document lands when it was started from a row in the tree.

Pressing ``+`` on a folder and then finding the document at the top
level is the failure this guards against, and it is a quiet one: the
add succeeded, the message said so, and the only thing wrong is that
the document is not where it was put. In a tree of any size that is
indistinguishable from it not having been created.

So the parent is set as part of the add rather than by a move the
panel makes afterwards. And when the filing fails, the message says
so — the document exists either way, and a reader who is not told
goes looking for it in the folder.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.backend.server_artifacts import ArtifactController


class FakeStore:
    def __init__(self, *, ok: bool = True, raises: bool = False) -> None:
        self.ok = ok
        self.raises = raises
        self.filed: list[tuple[str, str]] = []

    async def set_parent(self, *, node_id: str, parent_id: str | None) -> bool:
        if self.raises:
            raise RuntimeError("bolt is down")
        self.filed.append((node_id, parent_id or ""))
        return self.ok


class FakeManager:
    """A create that succeeds and reports the id it made."""

    def __init__(self, *, artifact_id: str | None = "new-1") -> None:
        self.created: list[str] = []
        self._artifact_id = artifact_id

    async def create(self, *, name: str, content: str, **_) -> SimpleNamespace:
        self.created.append(name)
        return SimpleNamespace(
            success=True,
            message="Created.",
            error=None,
            artifact_id=self._artifact_id,
        )


def controller(*, store: FakeStore, artifact_id: str | None = "new-1") -> ArtifactController:
    session = SimpleNamespace(
        artifact_mgr=FakeManager(artifact_id=artifact_id),
        artifacts=SimpleNamespace(client=store),
    )
    return ArtifactController(session)


class TestItGoesWhereItWasStarted:
    @pytest.mark.asyncio
    async def test_a_parent_is_set_on_the_new_document(self):
        store = FakeStore()
        ctl = controller(store=store)

        info = await ctl.add("# Retry guide\n\nBody.", parent_id="folder-7")

        assert store.filed == [("new-1", "folder-7")]
        assert "Filed where you asked" in info.text

    @pytest.mark.asyncio
    async def test_nothing_is_filed_without_a_parent(self):
        # The panel's own Add starts at the top level, and a document
        # with no parent is already a root. Calling ``set_parent``
        # with an empty id would be a write that means nothing.
        store = FakeStore()
        ctl = controller(store=store)

        await ctl.add("# Retry guide\n\nBody.")

        assert store.filed == []

    @pytest.mark.asyncio
    async def test_the_title_still_comes_from_the_first_line(self):
        # Placement must not change what the document is called.
        store = FakeStore()
        session = SimpleNamespace(
            artifact_mgr=FakeManager(),
            artifacts=SimpleNamespace(client=store),
        )
        ctl = ArtifactController(session)

        await ctl.add("# Retry guide\n\nBody.", parent_id="folder-7")

        assert session.artifact_mgr.created == ["Retry guide"]


class TestWhenFilingFails:
    @pytest.mark.asyncio
    async def test_a_refused_move_is_not_reported_as_filed(self):
        store = FakeStore(ok=False)
        ctl = controller(store=store)

        info = await ctl.add("# Retry guide\n\nBody.", parent_id="gone")

        assert "Filed where you asked" not in info.text

    @pytest.mark.asyncio
    async def test_a_store_that_throws_does_not_lose_the_document(self):
        # The add succeeded. Turning a filing error into an add error
        # would send the reader to write it again, and there would then
        # be two.
        store = FakeStore(raises=True)
        ctl = controller(store=store)

        info = await ctl.add("# Retry guide\n\nBody.", parent_id="folder-7")

        assert "top level" in info.text

    @pytest.mark.asyncio
    async def test_a_create_that_reports_no_id_files_nothing(self):
        # ``set_parent(node_id=None)`` would match no node, or worse,
        # match on a falsy id.
        store = FakeStore()
        ctl = controller(store=store, artifact_id=None)

        await ctl.add("# Retry guide\n\nBody.", parent_id="folder-7")

        assert store.filed == []
