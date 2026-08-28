"""End-to-end test: SessionOrchestrator.attach_neo4j swaps the
default session's knowledge + code_index backends to neo4j.

Neo4j is the DEFAULT storage — ``attach_neo4j`` fires unconditionally on
BE boot and constructs a :class:`Neo4jRuntime` that attaches to the
session's knowledge + code_index. The rare opt-out is
``EMBER_NEO4J_DISABLED=1``, which turns the attach into a no-op so
headless CI / test scenarios don't pay the JDK-download cost.

Live integration — requires ``NEO4J_TEST_URI``. Skipped otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from neo4j import AsyncGraphDatabase

from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.core.config.settings import Settings

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


def _make_orchestrator(tmp_path: Path) -> SessionOrchestrator:
    """Build a SessionOrchestrator with the bare minimum it needs.

    The orchestrator constructor takes a lot of dependencies
    (transport, login, rpc_router, etc.). The ones we need for
    ``attach_neo4j`` are ``backend`` (for ``self._backend``)
    and ``settings`` (for ``data_dir``). The rest can be
    ``MagicMock`` — ``attach_neo4j`` doesn't touch them.
    """
    # Real Session is heavy to construct (it spins up the
    # workspace, db, plugins, etc.). Use a minimal stub whose
    # ``knowledge`` field is a real ``None`` (we test the
    # swap-to-neo4j path; the chroma default is exercised by
    # the existing session tests).
    session = MagicMock()
    session.knowledge = None  # the swap target

    backend = MagicMock()
    backend._session = session
    # ``_backend.session_id`` and ``_backend.project_dir`` are
    # referenced by some code paths; supply them.
    backend.session_id = "sess-test"
    backend.project_dir = tmp_path

    return SessionOrchestrator(
        backend=backend,
        transport=MagicMock(),
        settings=Settings(),
        project_dir=tmp_path,
        additional_dirs=None,
        rpc_router=MagicMock(),
        rpc_table={},
        push_bridge=MagicMock(),
        login=MagicMock(),
        queue=[],
    )


async def test_orchestrator_attach_neo4j_no_op_when_disabled(tmp_path, monkeypatch):
    """With ``EMBER_NEO4J_DISABLED=1``, ``attach_neo4j`` is a no-op
    and no runtime is built."""
    monkeypatch.setenv("EMBER_NEO4J_DISABLED", "1")
    orch = _make_orchestrator(tmp_path)
    # The constructor doesn't construct the runtime (it's lazy).
    assert orch._neo4j_runtime is None

    result = await orch.attach_neo4j()
    assert result is None
    assert orch._neo4j_runtime is None
    # The session's knowledge is still ``None`` (the stub value).
    assert orch._backend._session.knowledge is None


async def test_orchestrator_attach_neo4j_swaps_knowledge_by_default(tmp_path, monkeypatch, driver):
    """Without ``EMBER_NEO4J_DISABLED``, ``attach_neo4j`` builds a
    runtime by default and calls ``Session.attach_knowledge_neo4j``
    + ``Session.attach_codeindex_neo4j``."""
    monkeypatch.delenv("EMBER_NEO4J_DISABLED", raising=False)

    # A real Session for the swap target (the stub's MagicMock
    # can't run the real attach path). Use a lightweight stand-in
    # that captures the runtime so we can verify the wiring.
    class _Session:
        def __init__(self, project_id: str):
            self._project_id = project_id
            # Start with a non-None knowledge + code_index (the
            # orchestrator only swaps when there's something to
            # swap; the real Session's constructor installs
            # chroma-backed defaults).
            self.knowledge = object()
            self.code_index = object()
            self.attached_runtimes: list[Any] = []
            self.codeindex_attach_calls = 0

        async def attach_knowledge_neo4j(self, runtime: Any) -> None:
            self.attached_runtimes.append(runtime)
            self.knowledge = f"neo4j-{runtime is not None}"

        async def attach_codeindex_neo4j(self, runtime: Any) -> None:
            self.codeindex_attach_calls += 1
            self.code_index = f"neo4j-codeindex-{runtime is not None}"

    project_id = "orch-attach-test"
    session = _Session(project_id)
    backend = MagicMock()
    backend._session = session
    backend.session_id = "sess-orch"
    backend.project_dir = tmp_path

    orch = SessionOrchestrator(
        backend=backend,
        transport=MagicMock(),
        settings=Settings(),
        project_dir=tmp_path,
        additional_dirs=None,
        rpc_router=MagicMock(),
        rpc_table={},
        push_bridge=MagicMock(),
        login=MagicMock(),
        queue=[],
    )

    runtime = await orch.attach_neo4j()
    assert runtime is not None
    # The runtime was cached on the orchestrator.
    assert orch._neo4j_runtime is runtime
    # The session received the runtime for BOTH the knowledge and
    # the code_index attach calls.
    assert session.attached_runtimes == [runtime]
    assert session.codeindex_attach_calls == 1
    # The session's knowledge + code_index were updated.
    assert session.knowledge == "neo4j-True"
    assert session.code_index == "neo4j-codeindex-True"


async def test_orchestrator_attach_neo4j_idempotent(tmp_path, monkeypatch, driver):
    """A second ``attach_neo4j`` call returns the cached runtime
    without rebuilding it."""
    monkeypatch.delenv("EMBER_NEO4J_DISABLED", raising=False)

    class _Session:
        def __init__(self):
            # Non-None so the orchestrator's swap-condition is met.
            self.knowledge = object()
            self.attach_calls = 0

        async def attach_knowledge_neo4j(self, runtime):
            self.attach_calls += 1
            self.knowledge = runtime

    session = _Session()
    backend = MagicMock()
    backend._session = session
    backend.session_id = "sess-id"
    backend.project_dir = tmp_path

    orch = SessionOrchestrator(
        backend=backend,
        transport=MagicMock(),
        settings=Settings(),
        project_dir=tmp_path,
        additional_dirs=None,
        rpc_router=MagicMock(),
        rpc_table={},
        push_bridge=MagicMock(),
        login=MagicMock(),
        queue=[],
    )

    first = await orch.attach_neo4j()
    second = await orch.attach_neo4j()

    # The runtime is cached — a second call returns the same one.
    assert first is second
    # The session's attach was called only once (the second call
    # short-circuited before reaching the session).
    assert session.attach_calls == 1


async def test_orchestrator_attach_neo4j_degrades_gracefully_on_construction_failure(
    tmp_path, monkeypatch
):
    """Runtime construction failures (missing Java, disk full, blocked
    download) must NOT crash BE boot — attach_neo4j logs + returns None
    so the session falls back to legacy Chroma-backed indices."""
    monkeypatch.delenv("EMBER_NEO4J_DISABLED", raising=False)

    # Patch the import inside attach_neo4j so construction raises.
    def _boom(**_kwargs):
        raise RuntimeError("simulated: cannot download Neo4j distribution")

    monkeypatch.setattr(
        "ember_code.backend.neo4j_runtime.Neo4jRuntime",
        _boom,
    )

    orch = _make_orchestrator(tmp_path)
    result = await orch.attach_neo4j()
    # Degradation: None, not raise.
    assert result is None
    # And nothing got cached — a later retry (with a fixed env) can succeed.
    assert orch._neo4j_runtime is None
