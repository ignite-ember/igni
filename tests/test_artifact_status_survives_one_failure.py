"""`artifact_status` must not report an empty store because one count failed.

The four figures shared one try/except, and the stale query asked the
server for 500 rows where `/v1/artifacts/stale` accepts at most 200. The
422 landed in the shared handler and zeroed every figure, so a signed-in
session always answered "Artifacts: 0, Versions: 0" beside a store with
documents in it — and an agent reading that concluded nothing was filed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.artifacts.session_manager import SessionArtifactManager


def _manager(index: MagicMock) -> SessionArtifactManager:
    settings = MagicMock()
    settings.artifacts.collection_name = "artifacts"
    return SessionArtifactManager(index, settings, Path("."))


def _index(**overrides) -> MagicMock:
    index = MagicMock()
    index.count = AsyncMock(return_value=3)
    index.version_count = AsyncMock(return_value=7)
    index.stale = AsyncMock(return_value=[{}, {}])
    index.candidate_count = AsyncMock(return_value=1)
    for name, value in overrides.items():
        setattr(index, name, value)
    return index


@pytest.mark.asyncio
async def test_a_failing_stale_query_costs_only_the_stale_figure():
    index = _index(stale=AsyncMock(side_effect=RuntimeError("422 Unprocessable Content")))

    status = await _manager(index).status()

    assert status.document_count == 3
    assert status.version_count == 7
    assert status.pending_candidates == 1
    assert status.stale_count == 0


@pytest.mark.asyncio
async def test_the_stale_query_stays_within_what_the_server_accepts():
    """`le=200` on /v1/artifacts/stale; asking for more is a 422."""
    index = _index()

    await _manager(index).status()

    assert index.stale.await_args.kwargs["limit"] <= 200


@pytest.mark.asyncio
async def test_every_figure_is_reported_when_nothing_fails():
    status = await _manager(_index()).status()

    assert (
        status.document_count,
        status.version_count,
        status.stale_count,
        status.pending_candidates,
    ) == (3, 7, 2, 1)
