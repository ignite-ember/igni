"""``/knowledge`` says which of four things is true, not two.

The panel's whole vocabulary for "there is no index" used to be
"Knowledge base failed to initialize." With the runtime enabled by
default and its setup off the boot path, a first launch spends minutes
in exactly that state while a ~500 MB download runs — and describing a
healthy download as a failure is the habit that made a silently
disabled knowledge base take an afternoon to diagnose.

The states come from the session's subsystem registry now, so the panel
reports what was recorded instead of inferring it from the absence of
data.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ember_code.backend.cmd_knowledge import KnowledgeCommand
from ember_code.backend.subsystem_status import KNOWLEDGE, SubsystemRegistry, SubsystemState


def _session(*, config_enabled: bool, ready: bool, record: tuple | None = None):
    """A session stub with only what ``panel()`` reads.

    ``record`` is ``(state, reason, fix)`` written into a real
    registry — a real one rather than a mock, because the registry's
    own rule (a failure must carry a reason) is part of what these
    assert.
    """
    session = MagicMock()
    session.settings = SimpleNamespace(knowledge=SimpleNamespace(enabled=config_enabled))
    session.knowledge_mgr.status = AsyncMock(return_value=SimpleNamespace(enabled=ready))
    registry = SubsystemRegistry()
    if record is not None:
        state, reason, fix = record
        registry.set(KNOWLEDGE, state, reason=reason, fix=fix)
    session.subsystems = registry
    return session


async def test_a_working_index_opens_the_panel():
    result = await KnowledgeCommand(_session(config_enabled=True, ready=True)).panel()

    assert result.action is not None


async def test_an_attach_still_running_reads_as_setup_not_failure():
    """The state that did not exist before, and the reason this file
    does. A user on a first launch sees this, not an error."""
    session = _session(
        config_enabled=True,
        ready=False,
        record=(SubsystemState.PREPARING, "starting the local database", ""),
    )

    result = await KnowledgeCommand(session).panel()

    assert "setting up" in result.content.lower()
    assert "fail" not in result.content.lower()
    assert "starting the local database" in result.content


async def test_a_failure_reports_its_reason_and_what_to_do():
    """Two strings, not one. "The sidecar exited immediately" is not
    something a user can act on; "delete ~/.ember/neo4j" is."""
    session = _session(
        config_enabled=True,
        ready=False,
        record=(
            SubsystemState.FAILED,
            "neo4j killed by SIGKILL (signal 9)",
            "Delete ~/.ember/neo4j and reopen the app.",
        ),
    )

    result = await KnowledgeCommand(session).panel()

    assert "neo4j killed by SIGKILL" in result.content
    assert "Delete ~/.ember/neo4j" in result.content


async def test_disabled_says_who_disabled_it():
    session = _session(
        config_enabled=False,
        ready=False,
        record=(
            SubsystemState.DISABLED,
            "knowledge.enabled is false in config",
            "Set knowledge.enabled to true in ~/.ember/config.yaml.",
        ),
    )

    result = await KnowledgeCommand(session).panel()

    assert "disabled" in result.content.lower()
    assert "knowledge.enabled" in result.content


async def test_a_session_with_no_record_falls_back_to_config():
    """Sessions built outside the backend's attach path — tests, an
    embedded Session — have nothing recorded. Better to repeat what
    config says than to invent a failure."""
    result = await KnowledgeCommand(_session(config_enabled=False, ready=False)).panel()

    assert "disabled" in result.content.lower()
