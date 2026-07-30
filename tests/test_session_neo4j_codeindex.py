"""End-to-end test: Session.attach_codeindex_neo4j swaps the
default code index for a neo4j backend.

Verifies the code-side analog of the knowledge wiring: when a
Session has its default chroma-backed code index, calling
:meth:`Session.attach_codeindex_neo4j` with a runtime
replaces ``self.code_index`` and the cached
``CodeIndexSyncManager`` with neo4j-backed equivalents, and
points the :class:`CodeIndexAvailabilityRefresher` at the new
refs.

Live integration — requires ``NEO4J_TEST_URI``. Skipped otherwise.
"""

from __future__ import annotations

import os

import pytest
from neo4j import AsyncGraphDatabase

from ember_code.core.config.settings import Settings
from ember_code.core.session import Session

_REQUIRED = "NEO4J_TEST_URI"


def _has_neo4j() -> bool:
    return bool(os.environ.get(_REQUIRED))


pytestmark = pytest.mark.skipif(not _has_neo4j(), reason=f"{_REQUIRED} not set")


@pytest.fixture
async def driver():
    d = AsyncGraphDatabase.driver(
        os.environ[_REQUIRED],
        auth=(
            os.environ.get("NEO4J_TEST_USER", "neo4j"),
            os.environ.get("NEO4J_TEST_PASSWORD", "test"),
        ),
    )
    try:
        yield d
    finally:
        await d.close()


class _StubRuntime:
    """Minimal ``Neo4jRuntime`` stand-in: every CodeIndex
    attach needs ``driver_for_commit`` + ``driver_for_knowledge``;
    the swap also constructs a new :class:`CodeIndexSyncManager`
    which calls ``current_sha()`` on the sync. We only need
    ``driver_for_commit`` (the index's runtime field is set
    directly; the sync talks to a different file)."""

    def __init__(self, driver) -> None:
        self._driver = driver
        self.driver_calls: list[str] = []

    def driver_for_commit(self, project_hash: str, commit_sha: str):
        self.driver_calls.append((project_hash, commit_sha))
        return self._driver


async def test_session_attach_codeindex_neo4j_routes_through_neo4j(tmp_path, driver):
    """Build a Session with the default chroma code index, swap
    in neo4j via ``attach_codeindex_neo4j``, and verify the
    underlying :class:`CodeIndex` carries the new runtime."""
    settings = Settings()
    session = Session(settings, project_dir=tmp_path)
    # The constructor's chroma-backed ``self.code_index`` is in place.
    assert session.code_index is not None
    assert getattr(session.code_index, "_neo4j_runtime", None) is None

    runtime = _StubRuntime(driver)
    await session.attach_codeindex_neo4j(runtime)

    # After attach: code_index is the neo4j-backed instance, the
    # _neo4j_runtime is the runtime we passed (so the
    # idempotency check works).
    assert session.code_index is not None
    assert getattr(session.code_index, "_neo4j_runtime", None) is runtime
    # The refreshers holds the new index too.
    if session._codeindex_refresher is not None:
        assert session._codeindex_refresher._code_index is session.code_index


async def test_session_attach_codeindex_neo4j_idempotent(tmp_path, driver):
    """A second ``attach_codeindex_neo4j`` with the same runtime
    does not rebuild the index."""
    settings = Settings()
    session = Session(settings, project_dir=tmp_path)
    runtime = _StubRuntime(driver)

    await session.attach_codeindex_neo4j(runtime)
    first = session.code_index

    await session.attach_codeindex_neo4j(runtime)
    second = session.code_index

    # The reference is unchanged — the idempotency guard
    # detected the same runtime and skipped the rebuild.
    assert second is first
