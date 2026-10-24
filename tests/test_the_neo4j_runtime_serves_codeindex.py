"""A session's Neo4j runtime is CodeIndex's, and only CodeIndex decides it.

Neo4j once backed two things in a session: the code index and a
document store. The documents are an extension now, and an extension
that needs a graph is given one of its own by the host's capabilities —
so the session runtime comes up when ``code_index.enabled`` says so,
with ``IGNI_NEO4J_RUNTIME`` as a process-level override both ways.

The runtime is stubbed, so these run everywhere: constructing the real
one downloads a JDK and a Neo4j distribution, and nothing in the
decisions here needs a database.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from ember_code.backend import neo4j_runtime as neo4j_runtime_mod
from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.core.config.settings import Settings


class _StubRuntime:
    def __init__(self, *_args: Any, **kwargs: Any) -> None:
        self.data_dir = kwargs.get("data_dir")


class _Session:
    """A session in the state the real constructor leaves behind."""

    def __init__(self) -> None:
        #: Built eagerly by the constructor, so non-None.
        self.code_index = object()
        self.codeindex_attach_calls = 0

    async def attach_codeindex_neo4j(self, runtime: Any) -> None:
        self.codeindex_attach_calls += 1


def _orchestrator(session: _Session, tmp_path, settings: Settings) -> SessionOrchestrator:
    backend = MagicMock()
    backend._session = session
    backend.session_id = "sess-neo4j"
    backend.project_dir = tmp_path
    return SessionOrchestrator(
        backend=backend,
        transport=MagicMock(),
        settings=settings,
        project_dir=tmp_path,
        additional_dirs=None,
        rpc_router=MagicMock(),
        rpc_table={},
        push_bridge=MagicMock(),
        login=MagicMock(),
        queue=[],
    )


def _settings(*, code_index: bool) -> Settings:
    settings = Settings()
    object.__setattr__(settings.code_index, "enabled", code_index)
    return settings


async def test_codeindex_on_builds_it_and_attaches_the_index(tmp_path, monkeypatch):
    monkeypatch.setattr(neo4j_runtime_mod, "Neo4jRuntime", _StubRuntime)
    monkeypatch.delenv("IGNI_NEO4J_RUNTIME", raising=False)
    session = _Session()

    runtime = await _orchestrator(session, tmp_path, _settings(code_index=True)).attach_neo4j()

    assert runtime is not None
    assert session.codeindex_attach_calls == 1


async def test_codeindex_off_builds_nothing(tmp_path, monkeypatch):
    """Nothing else gets a vote: no document store keeps the runtime up."""
    monkeypatch.setattr(neo4j_runtime_mod, "Neo4jRuntime", _StubRuntime)
    monkeypatch.delenv("IGNI_NEO4J_RUNTIME", raising=False)
    session = _Session()

    runtime = await _orchestrator(session, tmp_path, _settings(code_index=False)).attach_neo4j()

    assert runtime is None
    assert session.codeindex_attach_calls == 0


async def test_every_session_gets_it_not_just_the_boot_session(tmp_path, monkeypatch):
    """``attach_neo4j`` runs once at boot and reaches the boot session.
    Every session the user opens is built later by ``_create_runtime``,
    which shares ``_attach_neo4j_to`` — so a session handed over from the
    factory side is wired up too."""
    monkeypatch.setattr(neo4j_runtime_mod, "Neo4jRuntime", _StubRuntime)
    monkeypatch.delenv("IGNI_NEO4J_RUNTIME", raising=False)
    boot = _Session()
    orch = _orchestrator(boot, tmp_path, _settings(code_index=True))
    await orch.attach_neo4j()

    later = _Session()
    await orch._attach_neo4j_to(later)

    assert boot.codeindex_attach_calls == 1
    assert later.codeindex_attach_calls == 1


async def test_the_override_suppresses_it(tmp_path, monkeypatch):
    """The escape hatch, for a machine whose sidecar will not start."""
    monkeypatch.setattr(neo4j_runtime_mod, "Neo4jRuntime", _StubRuntime)
    monkeypatch.setenv("IGNI_NEO4J_RUNTIME", "0")
    session = _Session()

    runtime = await _orchestrator(session, tmp_path, _settings(code_index=True)).attach_neo4j()

    assert runtime is None
    assert session.codeindex_attach_calls == 0


async def test_the_override_forces_it(tmp_path, monkeypatch):
    """CI pinning a run: the override wins in both directions."""
    monkeypatch.setattr(neo4j_runtime_mod, "Neo4jRuntime", _StubRuntime)
    monkeypatch.setenv("IGNI_NEO4J_RUNTIME", "1")

    runtime = await _orchestrator(_Session(), tmp_path, _settings(code_index=False)).attach_neo4j()

    assert runtime is not None


async def test_a_failing_attach_does_not_take_the_backend_down(tmp_path, monkeypatch):
    """Boot starts this in the background; an exception in it must stay
    there rather than surface as an unretrieved task error or a crash."""

    class _Exploding(_Session):
        async def attach_codeindex_neo4j(self, runtime: Any) -> None:
            raise RuntimeError("neo4j would not start")

    monkeypatch.setattr(neo4j_runtime_mod, "Neo4jRuntime", _StubRuntime)
    monkeypatch.delenv("IGNI_NEO4J_RUNTIME", raising=False)

    task = _orchestrator(
        _Exploding(), tmp_path, _settings(code_index=True)
    ).attach_neo4j_in_background()
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert task.exception() is None
