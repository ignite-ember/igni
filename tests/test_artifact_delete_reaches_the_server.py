"""A delete removes the server's copy first, or does not happen at all.

It used to remove only the local graph. The tool reported "Deleted …
and its version history" while the server kept the document — still in
search, still open to everyone it was shared with, not even archived.
`artifact_read` then said it did not exist while `artifact_search`
kept finding it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.artifacts.errors import DeleteRefused
from ember_code.core.artifacts.index import ArtifactIndex


def _index(shared) -> ArtifactIndex:
    index = ArtifactIndex.__new__(ArtifactIndex)
    index._client = MagicMock()
    index._client.delete_artifact = AsyncMock(return_value=True)
    index._ledger = MagicMock()
    index._shared = shared
    index.resolve = AsyncMock(return_value=SimpleNamespace(artifact_id="a1", name="Release plan"))
    return index


def _shared(revoke) -> MagicMock:
    shared = MagicMock()
    shared.is_configured = True
    shared.revoke = revoke
    return shared


@pytest.mark.asyncio
async def test_the_server_copy_is_removed_before_the_local_one():
    order: list[str] = []
    shared = _shared(AsyncMock(side_effect=lambda **_: order.append("server") or True))
    index = _index(shared)
    index._client.delete_artifact = AsyncMock(side_effect=lambda _: order.append("local") or True)

    assert await index.delete("a1") is True
    assert order == ["server", "local"]


@pytest.mark.asyncio
async def test_a_server_failure_deletes_nothing_locally():
    """The split-brain this fix exists to prevent, from the other side."""
    index = _index(_shared(AsyncMock(side_effect=RuntimeError("503"))))

    with pytest.raises(DeleteRefused, match="Nothing was deleted here either"):
        await index.delete("a1")
    index._client.delete_artifact.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_session_not_signed_in_is_refused_not_half_done():
    signed_out = MagicMock()
    signed_out.is_configured = False
    index = _index(signed_out)

    with pytest.raises(DeleteRefused, match="not signed in"):
        await index.delete("a1")
    index._client.delete_artifact.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_tool_reports_the_refusal_instead_of_claiming_success():
    from ember_code.core.tools.artifacts import ArtifactTools

    mgr = MagicMock()
    mgr.delete = AsyncMock(
        side_effect=DeleteRefused("'Release plan' was not deleted: the server refused.")
    )

    out = await ArtifactTools(mgr).artifact_delete(ref="a1", confirm=True)

    assert out.startswith("Error:")
    assert "Deleted" not in out.split(":", 1)[1].split("was not")[0]
