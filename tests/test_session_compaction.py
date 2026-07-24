"""Unit tests for ``session/compaction/`` — the three
collaborators that own the compaction lifecycle.

Pins:

* :class:`CompactionCoordinator` — the auto-compact 80%
  threshold, the PreCompact / PostCompact hook fires, the
  ``CompactResult`` envelope returned by both entry points.
* :class:`FallbackSummariser` — transcript-empty short-circuit,
  the ``<think>``-tag strip, the two-shape (attribute vs dict)
  message row handling.
* :class:`ContextBreakdown.from_totals` — the floor-clamp
  invariant that keeps the ``/ctx`` panel well-formed even
  when the tokenizer mis-reports ``runs > total``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.hooks.schemas import HookResult
from ember_code.core.session.compaction import (
    CompactionCoordinator,
    FallbackSummariser,
    TranscriptMessage,
)
from ember_code.core.session.schemas import CompactResult, ContextBreakdown


def _bare_session(*, agno_session=None, hook_result=None):
    """Session-shaped stub carrying only what the coordinator reads."""
    session = SimpleNamespace()
    session.session_id = "sess-1"
    session.user_id = "user-1"
    session.main_team = SimpleNamespace()
    session.main_team.aget_session = AsyncMock(return_value=agno_session)
    session.main_team.asave_session = AsyncMock()
    session.main_team.model = SimpleNamespace()
    session.main_team.model.count_tokens = MagicMock(return_value=100)
    # Compaction now goes through the public rebuild seam.
    session.rebuild_main_team = MagicMock()
    session.hook_executor = SimpleNamespace()
    session.hook_executor.execute = AsyncMock(
        return_value=hook_result or HookResult(should_continue=True)
    )
    return session


class TestCompactIfNeeded:
    @pytest.mark.asyncio
    async def test_below_80_percent_is_noop(self):
        # Auto-compaction only fires at 80% usage. Below that, we
        # let the model chew through the context normally.
        s = _bare_session()
        coord = CompactionCoordinator(s)
        assert await coord.compact_if_needed(input_tokens=1000, context_window=10_000) is False
        s.hook_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_80_percent_triggers_compact(self):
        # Right at threshold — the ``usage < 0.8`` guard should
        # let 80% through as a trigger.
        s = _bare_session(agno_session=None)
        coord = CompactionCoordinator(s)
        # 80% of 10_000 = 8000
        result = await coord.compact_if_needed(input_tokens=8000, context_window=10_000)
        # ``compact()`` returned early ("Session not found") but
        # ``compact_if_needed`` still returns True since the
        # trigger fired.
        assert result is True

    @pytest.mark.asyncio
    async def test_precompact_hook_can_cancel(self):
        # A plugin can veto the compaction — critical escape hatch
        # for e.g. an "unsaved changes" guard.
        s = _bare_session(hook_result=HookResult(should_continue=False, message="blocked"))
        coord = CompactionCoordinator(s)
        result = await coord.compact_if_needed(input_tokens=9000, context_window=10_000)
        assert result is False
        # ``compact()`` never called → aget_session count is 0
        # (only the hook executor fired).
        s.main_team.aget_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_context_window_is_noop(self):
        s = _bare_session()
        coord = CompactionCoordinator(s)
        assert await coord.compact_if_needed(input_tokens=1000, context_window=0) is False

    @pytest.mark.asyncio
    async def test_fires_post_compact_hook_on_success(self):
        # PostCompact fires so plugins can react even when the
        # compaction is auto-triggered.
        s = _bare_session(agno_session=SimpleNamespace(runs=[], summary=None))
        coord = CompactionCoordinator(s)
        await coord.compact_if_needed(input_tokens=9000, context_window=10_000)
        events = [c.kwargs.get("event") for c in s.hook_executor.execute.call_args_list]
        assert "PreCompact" in events
        assert "PostCompact" in events


class TestForceCompact:
    @pytest.mark.asyncio
    async def test_empty_session_returns_nothing_to_compact(self):
        # No runs on the session → no history to summarise.
        empty_session = SimpleNamespace(runs=[], summary=None)
        s = _bare_session(agno_session=empty_session)
        coord = CompactionCoordinator(s)
        result = await coord.force_compact()
        assert isinstance(result, CompactResult)
        assert result.ok is False
        assert "Nothing to compact" in result.status
        assert result.summary == ""

    @pytest.mark.asyncio
    async def test_precompact_hook_can_cancel(self):
        # Same veto path as auto-compact, but for /compact.
        agno = SimpleNamespace(runs=[SimpleNamespace(messages=[])], summary=None)
        s = _bare_session(
            agno_session=agno,
            hook_result=HookResult(should_continue=False, message="unsaved changes"),
        )
        coord = CompactionCoordinator(s)
        result = await coord.force_compact()
        assert isinstance(result, CompactResult)
        assert result.ok is False
        assert "unsaved changes" in result.status or "cancelled" in result.status.lower()
        assert result.summary == ""


class TestContextBreakdown:
    @pytest.mark.asyncio
    async def test_no_session_returns_zeros(self):
        # Fresh session, never persisted — no history yet.
        s = _bare_session(agno_session=None)
        coord = CompactionCoordinator(s)
        result = await coord.context_breakdown()
        assert result.total == 0
        assert result.runs == 0
        assert result.floor == 0

    @pytest.mark.asyncio
    async def test_decomposes_total_into_runs_plus_floor(self):
        # The invariant that /ctx relies on: total == runs + floor.
        # ``floor`` is what /compact can't shrink (system prompt,
        # tool schemas, project rules).
        agno = SimpleNamespace()
        agno.runs = [SimpleNamespace(messages=[MagicMock(), MagicMock()])]
        agno.get_messages = MagicMock(return_value=[MagicMock() for _ in range(5)])
        s = _bare_session(agno_session=agno)
        coord = CompactionCoordinator(s)
        # Model reports different counts for the two calls (total
        # messages vs. runs-only messages).
        s.main_team.model.count_tokens = MagicMock(side_effect=[500, 200])
        result = await coord.context_breakdown()
        assert result.total == 500
        assert result.runs == 200
        assert result.floor == 300  # 500 - 200

    @pytest.mark.asyncio
    async def test_floor_never_negative(self):
        # If tokenizer bugs make ``runs`` > ``total``, floor
        # should clamp to 0 rather than go negative (the /ctx
        # panel renders a negative pill oddly).
        agno = SimpleNamespace()
        agno.runs = [SimpleNamespace(messages=[MagicMock()])]
        agno.get_messages = MagicMock(return_value=[MagicMock()])
        s = _bare_session(agno_session=agno)
        s.main_team.model.count_tokens = MagicMock(side_effect=[10, 20])
        coord = CompactionCoordinator(s)
        result = await coord.context_breakdown()
        assert result.floor == 0


class TestContextBreakdownFromTotals:
    def test_floor_clamped_to_zero(self):
        # Runs > total is a tokenizer bug, but the model must
        # still land at a well-formed shape.
        b = ContextBreakdown.from_totals(total=10, runs=20)
        assert b.total == 10
        assert b.runs == 20
        assert b.floor == 0

    def test_normal_case_preserves_invariant(self):
        b = ContextBreakdown.from_totals(total=500, runs=200)
        assert b.floor == 300
        assert b.total == b.runs + b.floor


class TestFallbackSummariser:
    def _stub_session(self, *, aresponse_return=None, aresponse_side=None):
        session = SimpleNamespace()
        session.main_team = SimpleNamespace()
        session.main_team.model = SimpleNamespace()
        if aresponse_side is not None:
            session.main_team.model.aresponse = AsyncMock(side_effect=aresponse_side)
        else:
            session.main_team.model.aresponse = AsyncMock(return_value=aresponse_return)
        return session

    @pytest.mark.asyncio
    async def test_empty_transcript_short_circuits(self):
        # No runs → no transcript → no model call.
        session = self._stub_session(aresponse_return=SimpleNamespace(content="x"))
        agno = SimpleNamespace(runs=[])
        summ = FallbackSummariser(session)
        out = await summ.summarise(agno)
        assert out == ""
        session.main_team.model.aresponse.assert_not_called()

    @pytest.mark.asyncio
    async def test_strips_think_tags(self):
        # MiniMax leaks ``<think>`` blocks — the summariser must
        # scrub them defensively.
        session = self._stub_session(
            aresponse_return=SimpleNamespace(content="<think>ignore me</think>Final answer.")
        )
        agno = SimpleNamespace(
            runs=[
                SimpleNamespace(
                    messages=[
                        SimpleNamespace(role="user", content="hi"),
                        SimpleNamespace(role="assistant", content="hello"),
                    ]
                )
            ]
        )
        summ = FallbackSummariser(session)
        out = await summ.summarise(agno)
        assert "<think>" not in out
        assert "Final answer." in out

    @pytest.mark.asyncio
    async def test_non_string_response_returns_empty(self):
        session = self._stub_session(aresponse_return=SimpleNamespace(content=None))
        agno = SimpleNamespace(
            runs=[SimpleNamespace(messages=[SimpleNamespace(role="user", content="hi")])]
        )
        summ = FallbackSummariser(session)
        assert await summ.summarise(agno) == ""

    @pytest.mark.asyncio
    async def test_aresponse_failure_returns_empty(self):
        session = self._stub_session(aresponse_side=RuntimeError("boom"))
        agno = SimpleNamespace(
            runs=[SimpleNamespace(messages=[SimpleNamespace(role="user", content="hi")])]
        )
        summ = FallbackSummariser(session)
        assert await summ.summarise(agno) == ""


class TestTranscriptMessage:
    def test_from_attribute_shaped_message(self):
        raw = SimpleNamespace(role="user", content="hi")
        msg = TranscriptMessage.from_agno_message(raw)
        assert msg is not None
        assert msg.role == "user"
        assert msg.content == "hi"

    def test_from_dict_shaped_row(self):
        # Older DB rows arrive as plain dicts.
        raw = {"role": "assistant", "content": "  hello  "}
        msg = TranscriptMessage.from_agno_message(raw)
        assert msg is not None
        assert msg.role == "assistant"
        # ``content`` is stripped so the transcript line is clean.
        assert msg.content == "hello"

    def test_non_user_assistant_role_filtered(self):
        raw = SimpleNamespace(role="tool", content="{}")
        assert TranscriptMessage.from_agno_message(raw) is None

    def test_empty_content_filtered(self):
        raw = SimpleNamespace(role="user", content="   ")
        assert TranscriptMessage.from_agno_message(raw) is None

    def test_non_string_content_filtered(self):
        raw = SimpleNamespace(role="assistant", content={"blob": True})
        assert TranscriptMessage.from_agno_message(raw) is None
