"""The knowledge attach must fire for a session that has no index yet.

Every session the BE builds starts with ``knowledge = None``. The chroma
fallback is gone, so ``Session.__init__`` defers the index and
``SessionOrchestrator.attach_neo4j`` is what installs it.

The guard there used to read "attach only if an index is already
present", which for a BE session was never true — the call that creates
knowledge was gated on knowledge already existing. Every session came up
with no index, and the panel said "Knowledge base disabled" no matter
what the settings held.

``tests/test_orchestrator_attach_neo4j.py`` covers the same call but is
skipped wholesale unless ``NEO4J_TEST_URI`` is set, and its fakes start
``knowledge`` as a sentinel object that no real session ever has — so it
stayed green throughout. These tests take neither shortcut: the runtime
is stubbed, so they run everywhere, and the session starts in the state
the real constructor leaves it in.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ember_code.backend import neo4j_runtime as neo4j_runtime_mod
from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.core.config.settings import Settings


class _StubRuntime:
    """Stands in for ``Neo4jRuntime``.

    Constructing the real one downloads a JDK and a Neo4j distribution,
    which is why the sibling module hides behind an env var. Nothing in
    the decision under test needs a database — only whether the attach
    is reached — so the runtime is replaced rather than required.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.data_dir = _kwargs.get("data_dir")


class _Session:
    """A session in the state the real constructor leaves behind."""

    def __init__(self) -> None:
        #: What ``Session.__init__`` sets when no runtime exists yet.
        self.knowledge = None
        #: Still built eagerly by the constructor, so non-None here.
        self.code_index = object()
        self.knowledge_attach_calls = 0
        self._knowledge_error: str | None = None
        self.codeindex_attach_calls = 0

    async def attach_knowledge_neo4j(self, runtime: Any) -> None:
        self.knowledge_attach_calls += 1
        self.knowledge = runtime

    async def attach_codeindex_neo4j(self, runtime: Any) -> None:
        self.codeindex_attach_calls += 1


@pytest.fixture
def stub_runtime(monkeypatch):
    monkeypatch.setattr(neo4j_runtime_mod, "Neo4jRuntime", _StubRuntime)
    monkeypatch.setenv("EMBER_NEO4J_RUNTIME", "1")


def _orchestrator(session: _Session, tmp_path, settings: Settings) -> SessionOrchestrator:
    backend = MagicMock()
    backend._session = session
    backend.session_id = "sess-knowledge"
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


async def test_a_session_with_no_index_still_gets_one(tmp_path, stub_runtime):
    session = _Session()
    assert session.knowledge is None, "the precondition the old guard could not survive"

    await _orchestrator(session, tmp_path, Settings()).attach_neo4j()

    assert session.knowledge_attach_calls == 1
    assert session.knowledge is not None


async def test_the_setting_still_switches_it_off(tmp_path, stub_runtime):
    """Gating on the setting rather than on an existing index must not
    turn ``knowledge.enabled = false`` into an attach — otherwise the
    fix trades a switch stuck off for one stuck on."""
    settings = Settings()
    settings.knowledge.enabled = False
    session = _Session()

    await _orchestrator(session, tmp_path, settings).attach_neo4j()

    assert session.knowledge_attach_calls == 0
    assert session.knowledge is None


async def test_the_code_index_attach_is_untouched(tmp_path, stub_runtime):
    """``code_index`` keeps its ``is not None`` guard, because its
    constructor really does build one. Stated here so a later tidy-up
    does not "consistently" convert it to a settings check and quietly
    change when the code index attaches."""
    session = _Session()

    await _orchestrator(session, tmp_path, Settings()).attach_neo4j()

    assert session.codeindex_attach_calls == 1


async def test_every_session_gets_one_not_just_the_boot_session(tmp_path, stub_runtime):
    """The half that survived the first fix.

    ``attach_neo4j`` runs once at boot and reaches
    ``self._backend._session``. Every session the user actually opens is
    built later by ``_create_runtime``, which makes its own
    ``BackendServer`` and its own ``Session`` — so fixing only the boot
    path left the panel reporting failure on exactly the sessions people
    use. ``_attach_neo4j_to`` is shared by both call sites; this pins
    that a session handed to it from the factory side is wired up too.
    """
    boot = _Session()
    orch = _orchestrator(boot, tmp_path, Settings())
    await orch.attach_neo4j()
    assert boot.knowledge_attach_calls == 1

    # A second session, as `_create_runtime` would hand it over.
    later = _Session()
    await orch._attach_neo4j_to(later)

    assert later.knowledge_attach_calls == 1, "a session from the factory must be attached too"
    assert later.knowledge is not None


async def test_a_failing_attach_does_not_take_the_backend_down(tmp_path, stub_runtime, caplog):
    """Boot awaits this. An exception here used to mean no backend.

    The first live run of this path died on
    ``Neo4jRuntime.start_for_knowledge(...) must be called before
    driver_for_knowledge(...)`` — raised out of ``attach_knowledge_neo4j``,
    through ``BackendApp.run``, and the process exited. The panel not
    working is a bad afternoon; the BE not starting is a broken app.
    """

    class _Exploding(_Session):
        async def attach_knowledge_neo4j(self, runtime: Any) -> None:
            raise RuntimeError("neo4j would not start")

    session = _Exploding()
    await _orchestrator(session, tmp_path, Settings()).attach_neo4j()

    assert session.knowledge is None
    # The reason reaches the field the panel reads.
    assert "neo4j would not start" in (session._knowledge_error or "")
    # The code index still gets its turn — one failure is not all of them.
    assert session.codeindex_attach_calls == 1


async def test_nothing_attaches_without_the_opt_in(tmp_path, monkeypatch):
    """The env var stays the opt-in. The fix is about which sessions
    qualify once it is set, not about setting it for them."""
    monkeypatch.setattr(neo4j_runtime_mod, "Neo4jRuntime", _StubRuntime)
    monkeypatch.delenv("EMBER_NEO4J_RUNTIME", raising=False)
    session = _Session()

    runtime = await _orchestrator(session, tmp_path, Settings()).attach_neo4j()

    assert runtime is None
    assert session.knowledge_attach_calls == 0
