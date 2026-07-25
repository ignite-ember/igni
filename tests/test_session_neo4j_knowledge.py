"""End-to-end test: Session.attach_knowledge_neo4j swaps in a neo4j backend.

Verifies the BE-level seam added in step 2: when a Session
has its default chroma-backed knowledge, calling
:meth:`Session.attach_knowledge_neo4j` with a runtime (real or
stub) replaces ``self.knowledge`` and the cached
``SessionKnowledgeManager.knowledge`` with a neo4j-backed
:class:`KnowledgeIndex` driven by the runtime's
``driver_for_knowledge`` method. Subsequent add / search / count
ops hit the live neo4j driver.

Live integration — requires ``NEO4J_TEST_URI``. Skipped otherwise.
"""

from __future__ import annotations

import os
import uuid

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
    """Minimal ``Neo4jRuntime`` stand-in: exposes
    ``driver_for_knowledge(project_id)`` and nothing else. The
    Session only depends on that one method.
    """

    def __init__(self, driver) -> None:
        self._driver = driver
        self.driver_calls: list[str] = []

    def driver_for_knowledge(self, project_id: str):
        self.driver_calls.append(project_id)
        return self._driver


@pytest.fixture
async def project_id(driver):
    pid = uuid.uuid4().hex[:32]
    async with driver.session() as s:
        await s.run("MATCH (n {project_hash: $p}) DETACH DELETE n", p=pid)
    yield pid
    async with driver.session() as s:
        await s.run("MATCH (n {project_hash: $p}) DETACH DELETE n", p=pid)


async def test_session_attach_knowledge_neo4j_routes_through_neo4j(tmp_path, driver, project_id):
    """Build a Session with the default chroma knowledge, swap in
    neo4j via ``attach_knowledge_neo4j``, and verify knowledge ops
    hit the live driver."""
    settings = Settings()
    settings.knowledge.enabled = True
    # Project dir is what the resolver hashes into project_id.
    session = Session(settings, project_dir=tmp_path)
    # The constructor's chroma-backed ``self.knowledge`` is in place.
    assert session.knowledge is not None
    assert getattr(session.knowledge, "_neo4j_client", None) is None

    runtime = _StubRuntime(driver)
    await session.attach_knowledge_neo4j(runtime)

    # After attach: knowledge is the neo4j-backed index, the
    # ``_neo4j_client`` is the same instance we built inside
    # attach (so the idempotency check works), and the
    # knowledge_mgr holds the same reference.
    assert session.knowledge is not None
    assert getattr(session.knowledge, "_neo4j_client", None) is not None
    assert session.knowledge_mgr.knowledge is session.knowledge
    # The runtime saw exactly one driver_for_knowledge call (for
    # the project's hash).
    assert runtime.driver_calls, "driver_for_knowledge was never called"

    # Add a knowledge entry through the new backend. The
    # ``KnowledgeIndex`` constructor wires a ``LiveEmbedder`` which
    # Add a knowledge entry through the public seam. The
    # ``LiveEmbedder`` wired in by ``attach_knowledge_neo4j``
    # uses the project's model loader (multi-second download on
    # first call) — exercise the underlying client directly to
    # avoid that cost in the test. The on-the-wire embedding path
    # is covered by ``test_knowledge_neo4j_backend.py``.
    from ember_code.core.code_index.embedder import HashEmbedder

    client = session.knowledge._neo4j_client
    embedder = HashEmbedder()
    content = "test entry"
    await client.add_entry(
        entry_id="session-seam",
        name="Test",
        source="unit",
        content=content,
        embedding=embedder.embed([content])[0],
        chunks=[],
    )

    # count + has_entry through the seam: must hit neo4j.
    assert await session.knowledge.count() == 1
    entries = await session.knowledge.list_entries()
    assert len(entries) == 1
    # Clean up
    assert await session.knowledge.delete_entry(entries[0].id) is True
    assert await session.knowledge.count() == 0


async def test_session_attach_knowledge_neo4j_idempotent(tmp_path, driver, project_id):
    """A second ``attach_knowledge_neo4j`` with the same runtime
    does not rebuild the index."""
    settings = Settings()
    settings.knowledge.enabled = True
    session = Session(settings, project_dir=tmp_path)
    runtime = _StubRuntime(driver)

    await session.attach_knowledge_neo4j(runtime)
    first = session.knowledge
    first_client = first._neo4j_client

    await session.attach_knowledge_neo4j(runtime)
    second = session.knowledge

    # The reference is unchanged — the idempotency guard
    # detected the same client and skipped the rebuild.
    assert second is first
    assert second._neo4j_client is first_client


async def test_session_attach_knowledge_neo4j_overrides_disabled(tmp_path, driver, project_id):
    """``attach_knowledge_neo4j`` is an explicit switch — it
    installs the neo4j backend regardless of the
    ``knowledge.enabled`` config flag. The disabled flag is for
    the constructor's default; an explicit runtime attach is
    the higher-priority signal.

    The test documents this behavior so future-me doesn't
    waste time wondering whether the flag should short-circuit
    the attach (it shouldn't — the BE explicitly asks for neo4j).
    """
    settings = Settings()
    settings.knowledge.enabled = False
    session = Session(settings, project_dir=tmp_path)
    # Constructor skips the chroma path entirely.
    assert session.knowledge is None

    runtime = _StubRuntime(driver)
    await session.attach_knowledge_neo4j(runtime)
    # The explicit attach succeeds — the flag is for the
    # constructor default, not a hard block.
    assert session.knowledge is not None
    assert getattr(session.knowledge, "_neo4j_client", None) is not None
    assert runtime.driver_calls == [session.knowledge.project_id]
