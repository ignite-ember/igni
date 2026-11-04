"""Compaction must never delete a conversation it cannot replace.

``CompactionCoordinator.compact`` dropped every run from the DB
unconditionally — including the pass where the structured summariser
errored AND the free-text fallback came back empty. Both of those are
transients the user cannot influence (a provider 5xx, an unparseable
response), and auto-compaction fires on its own at 80% context on every
path that calls ``compact_if_needed``. The result was a session that
deleted the whole conversation and put nothing in its place.

These tests pin the trade the coordinator now refuses to make: runs are
dropped only when a *new* summary exists to stand in for them, and
everything that mirrors the drop (``compact_if_needed``'s return, the
PostCompact hook, the ``/compact`` status line) reports what actually
happened.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.hooks.schemas import HookResult
from ember_code.core.session.compaction import CompactionCoordinator
from ember_code.core.session.compaction import coordinator as coordinator_mod


class _NullSummaryManager:
    """Agno's summariser, doing what a failing provider makes it do:
    returning without ever writing ``session.summary``."""

    def __init__(self, model=None) -> None:
        self.model = model

    async def acreate_session_summary(self, session):  # noqa: ANN001
        return None


class _RaisingSummaryManager(_NullSummaryManager):
    async def acreate_session_summary(self, session):  # noqa: ANN001
        raise RuntimeError("provider 503")


def _session_stub(agno_session):
    s = SimpleNamespace()
    s.session_id = "sess-1"
    s.user_id = "user-1"
    s.main_team = SimpleNamespace()
    s.main_team.aget_session = AsyncMock(return_value=agno_session)
    s.main_team.asave_session = AsyncMock()
    s.main_team.model = SimpleNamespace()
    s.rebuild_main_team = MagicMock()
    s.hook_executor = SimpleNamespace()
    s.hook_executor.execute = AsyncMock(return_value=HookResult(should_continue=True))
    return s


def _conversation(summary=None):
    """An agno session carrying real history worth losing."""
    return SimpleNamespace(runs=[SimpleNamespace(), SimpleNamespace()], summary=summary)


@pytest.fixture
def no_structured_summary(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "SessionSummaryManager", _NullSummaryManager)


class TestBothSummarisersEmpty:
    @pytest.mark.asyncio
    async def test_runs_are_not_dropped(self, no_structured_summary):
        agno = _conversation()
        s = _session_stub(agno)
        coord = CompactionCoordinator(s)
        coord._fallback.summarise = AsyncMock(return_value="")

        result = await coord.compact()

        assert agno.runs, "the conversation was deleted with nothing kept in its place"
        assert result.history_cleared is False
        assert result.ok is False
        s.main_team.asave_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_compact_reports_no_change(self, no_structured_summary):
        # ``compact_if_needed`` returning True makes the backend push a
        # ``SessionCleared`` to the FE. Nothing was cleared.
        s = _session_stub(_conversation())
        coord = CompactionCoordinator(s)
        coord._fallback.summarise = AsyncMock(return_value="")

        assert await coord.compact_if_needed(input_tokens=9000, context_window=10_000) is False

        events = [c.kwargs.get("event") for c in s.hook_executor.execute.call_args_list]
        assert "PreCompact" in events
        assert "PostCompact" not in events

    @pytest.mark.asyncio
    async def test_manual_compact_says_the_history_is_intact(self, monkeypatch):
        monkeypatch.setattr(coordinator_mod, "SessionSummaryManager", _RaisingSummaryManager)
        agno = _conversation()
        s = _session_stub(agno)
        coord = CompactionCoordinator(s)
        coord._fallback.summarise = AsyncMock(return_value="")

        result = await coord.force_compact()

        assert agno.runs
        assert result.ok is False
        assert result.history_cleared is False
        assert "untouched" in result.status
        assert result.error is not None and "provider 503" in result.error


class TestStaleSummary:
    @pytest.mark.asyncio
    async def test_a_failed_pass_does_not_trade_runs_for_the_old_summary(
        self, no_structured_summary
    ):
        # A second compaction whose summariser fails still finds the
        # FIRST pass's summary sitting on the session. That text says
        # nothing about the runs recorded since, so dropping them loses
        # them silently.
        agno = _conversation(summary=SimpleNamespace(summary="what happened before"))
        s = _session_stub(agno)
        coord = CompactionCoordinator(s)
        coord._fallback.summarise = AsyncMock(return_value="")

        result = await coord.compact()

        assert agno.runs
        assert result.history_cleared is False


class TestSummaryProduced:
    @pytest.mark.asyncio
    async def test_fallback_summary_still_clears_the_history(self, no_structured_summary):
        # The control: when something *does* replace the conversation,
        # compaction goes through exactly as before.
        agno = _conversation()
        s = _session_stub(agno)
        coord = CompactionCoordinator(s)
        coord._fallback.summarise = AsyncMock(return_value="a recap of the session")

        result = await coord.compact()

        assert agno.runs == []
        assert result.history_cleared is True
        assert result.summary == "a recap of the session"
        s.main_team.asave_session.assert_awaited_once()
        s.rebuild_main_team.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_save_failure_is_not_reported_as_cleared(self, no_structured_summary):
        # The runs are still in the DB, and the rebuilt agent reloads
        # them from there — so nothing was cleared, whatever the
        # in-memory object says.
        s = _session_stub(_conversation())
        s.main_team.asave_session = AsyncMock(side_effect=RuntimeError("db is locked"))
        coord = CompactionCoordinator(s)
        coord._fallback.summarise = AsyncMock(return_value="a recap")

        result = await coord.compact()

        assert result.history_cleared is False
