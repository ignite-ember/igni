"""``/knowledge`` says which of four things is true, not two.

The panel's whole vocabulary for "there is no index" used to be
"Knowledge base failed to initialize." With the runtime now enabled by
default and its setup moved off the boot path, a first launch spends
minutes in exactly that state while a ~500 MB download runs — and
describing a healthy download as a failure is the same habit that made
a silently-disabled knowledge base take an afternoon to diagnose.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ember_code.backend.cmd_knowledge import KnowledgeCommand


def _session(*, enabled: bool, ready: bool, preparing: bool = False, error: str | None = None):
    """A session stub with only what ``panel()`` reads."""
    session = MagicMock()
    session.settings = SimpleNamespace(knowledge=SimpleNamespace(enabled=enabled))
    session.knowledge_mgr.status = AsyncMock(return_value=SimpleNamespace(enabled=ready))
    session.knowledge_error = error
    session.knowledge_preparing = preparing
    return session


async def test_a_working_index_opens_the_panel():
    result = await KnowledgeCommand(_session(enabled=True, ready=True)).panel()

    assert result.action is not None


async def test_disabled_in_config_says_so():
    result = await KnowledgeCommand(_session(enabled=False, ready=False)).panel()

    assert "disabled" in result.content.lower()
    assert "knowledge.enabled" in result.content


async def test_an_attach_still_running_reads_as_setup_not_failure():
    """The state that did not exist before, and the reason this file
    does. A user on a first launch sees this, not an error."""
    result = await KnowledgeCommand(_session(enabled=True, ready=False, preparing=True)).panel()

    assert "setting up" in result.content.lower()
    assert "fail" not in result.content.lower()


async def test_a_real_failure_still_reports_its_reason():
    result = await KnowledgeCommand(
        _session(enabled=True, ready=False, error="no route to dist.neo4j.org")
    ).panel()

    assert "no route to dist.neo4j.org" in result.content


async def test_a_recorded_error_wins_over_preparing():
    """Both flags can be set if a failure lands before the task clears
    ``preparing``. The error is the more useful of the two."""
    result = await KnowledgeCommand(
        _session(enabled=True, ready=False, preparing=True, error="bolt port never opened")
    ).panel()

    assert "bolt port never opened" in result.content
