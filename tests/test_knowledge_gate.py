"""The knowledge switch is config, and boot does not wait for it.

Two facts this file pins down, both of which used to be false:

1. ``knowledge.enabled`` decides whether the Neo4j runtime comes up.
   It used to be ``IGNI_NEO4J_RUNTIME`` alone — an environment
   variable that silently overrode the config setting nine other call
   sites honour, and that a Finder-launched ``.app`` cannot be given.
2. Enabling it does not put a ~500 MB download between the user and a
   ready backend. The attach runs in the background; the panel says
   so while it does.

Unlike ``test_orchestrator_attach_neo4j.py``, nothing here needs a live
Neo4j — these are about the decision and the sequencing, not the
database. That file skips without ``NEO4J_TEST_URI``, which is how the
old gate's behaviour went unexamined for so long.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ember_code.backend.knowledge_gate import ENV_OVERRIDE, knowledge_runtime_enabled
from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.backend.subsystem_status import KNOWLEDGE, SubsystemRegistry, SubsystemState
from ember_code.core.config.settings import Settings


def _settings(*, enabled: bool, code_index: bool = False) -> Settings:
    s = Settings()
    s.knowledge.enabled = enabled
    # Neo4j backs CodeIndex too, so this has to be stated rather than left at
    # its default: with it on, the sidecar starts even when knowledge is off.
    s.code_index.enabled = code_index
    return s


# ── The gate ────────────────────────────────────────────────────────


def test_config_decides_when_no_override_is_set(monkeypatch):
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)

    assert knowledge_runtime_enabled(_settings(enabled=True)) is True
    assert knowledge_runtime_enabled(_settings(enabled=False)) is False


def test_knowledge_is_on_by_default():
    """The shipped default. Worth its own assertion because the whole
    bug was that config said this while the runtime said otherwise."""
    assert Settings().knowledge.enabled is True


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_the_override_can_force_it_on(monkeypatch, value):
    monkeypatch.setenv(ENV_OVERRIDE, value)

    assert knowledge_runtime_enabled(_settings(enabled=False)) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_the_override_can_force_it_off(monkeypatch, value):
    """The direction the old code had no way to express: setting the
    variable at all meant "on", so there was no escape hatch for
    someone whose sidecar will not start."""
    monkeypatch.setenv(ENV_OVERRIDE, value)

    assert knowledge_runtime_enabled(_settings(enabled=True)) is False


def test_a_settings_object_without_a_knowledge_section_is_not_a_crash(monkeypatch):
    """Called from the boot path: a malformed config should cost the
    user their knowledge base, not their backend."""
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)

    assert knowledge_runtime_enabled(object()) is False


# ── The sequencing ──────────────────────────────────────────────────


def _state(orch) -> SubsystemState | None:
    entry = orch._backend._session.subsystems.get(KNOWLEDGE)
    return entry.state if entry else None


def _make_orchestrator(
    tmp_path: Path, *, enabled: bool, code_index: bool = False
) -> SessionOrchestrator:
    session = MagicMock()
    session.knowledge = None
    session.subsystems = SubsystemRegistry()
    backend = MagicMock()
    backend._session = session
    backend.session_id = "sess-test"
    backend.project_dir = tmp_path
    return SessionOrchestrator(
        backend=backend,
        transport=MagicMock(),
        settings=_settings(enabled=enabled, code_index=code_index),
        project_dir=tmp_path,
        additional_dirs=None,
        rpc_router=MagicMock(),
        rpc_table={},
        push_bridge=MagicMock(),
        login=MagicMock(),
        queue=[],
    )


async def test_background_attach_returns_immediately_even_when_the_attach_is_slow(
    tmp_path, monkeypatch
):
    """The point of the change. A download that takes forever must not
    delay the caller — boot awaited this directly before, which was
    survivable only because the env gate meant it never ran."""
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)
    orch = _make_orchestrator(tmp_path, enabled=True)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_attach():
        started.set()
        await release.wait()

    monkeypatch.setattr(orch, "attach_neo4j", _slow_attach)

    task = orch.attach_neo4j_in_background()
    assert task is not None
    # The call returned while the attach is still running — that is the
    # whole assertion. `wait_for` would pass trivially if it had not.
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not task.done()
    assert _state(orch) is SubsystemState.PREPARING

    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert _state(orch) is SubsystemState.READY


async def test_background_attach_does_nothing_when_knowledge_is_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)
    orch = _make_orchestrator(tmp_path, enabled=False)

    called = False

    async def _attach():
        nonlocal called
        called = True

    monkeypatch.setattr(orch, "attach_neo4j", _attach)

    # ``None`` rather than a finished task, so a caller can tell "not
    # running" from "already done".
    assert orch.attach_neo4j_in_background() is None
    assert called is False
    # Off, and the record says who turned it off — "disabled" and
    # "broken" are the two states this panel used to conflate.
    entry = orch._backend._session.subsystems.get(KNOWLEDGE)
    assert entry.state is SubsystemState.DISABLED
    assert "knowledge.enabled" in entry.reason


async def test_the_sidecar_still_starts_for_codeindex(tmp_path, monkeypatch):
    """Knowledge off, CodeIndex on: the panel says disabled, the sidecar runs.

    Gating the sidecar on ``knowledge.enabled`` alone left ``codeindex_cypher``
    answering ``no_backend`` for a feature the user had switched on. Reporting
    it as ``PREPARING`` instead would be the opposite error — a knowledge panel
    that never resolves, for a subsystem the user turned off.
    """
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)
    orch = _make_orchestrator(tmp_path, enabled=False, code_index=True)

    called = False

    async def _attach():
        nonlocal called
        called = True

    monkeypatch.setattr(orch, "attach_neo4j", _attach)

    task = orch.attach_neo4j_in_background()
    assert task is not None
    await task
    assert called is True

    entry = orch._backend._session.subsystems.get(KNOWLEDGE)
    assert entry.state is SubsystemState.DISABLED
    assert "knowledge.enabled" in entry.reason


async def test_a_failing_attach_records_the_reason_and_clears_preparing(tmp_path, monkeypatch):
    """A background task that dies silently is the failure mode this
    whole area kept producing. The reason has to land where the panel
    already looks."""
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)
    orch = _make_orchestrator(tmp_path, enabled=True)
    session = orch._backend._session

    async def _boom():
        raise RuntimeError("no route to dist.neo4j.org")

    monkeypatch.setattr(orch, "attach_neo4j", _boom)

    task = orch.attach_neo4j_in_background()
    await asyncio.wait_for(task, timeout=1)

    entry = session.subsystems.get(KNOWLEDGE)
    assert entry.state is SubsystemState.FAILED
    assert "no route to dist.neo4j.org" in entry.reason
    # A reason without a remedy is half an answer.
    assert entry.fix
