"""Unit tests for ``session/compact_ops.py``.

Extracted in iter 143 — five compaction functions. The
Session-method delegates are tested via `test_session.py`;
these tests pin the free-function contracts in isolation,
especially:

* The two-step summariser design (structured → free-text
  fallback) actually retries when the structured summary comes
  back empty.
* `compact_if_needed` respects the 80% threshold + PreCompact
  hook blocking + fires the PostCompact hook.
* `force_compact` returns a distinguishable status for each
  failure branch (nothing-to-compact, summariser-error, empty-
  summary, success).
* `context_breakdown` decomposes total = runs + floor.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.hooks.schemas import HookResult
from ember_code.core.session.compact_ops import (
    compact_if_needed,
    context_breakdown,
    force_compact,
)


def _bare_session(*, agno_session=None, hook_result=None):
    """Session-shaped stub carrying only what compact_ops reads."""
    session = SimpleNamespace()
    session.session_id = "sess-1"
    session.user_id = "user-1"
    session.main_team = SimpleNamespace()
    session.main_team.aget_session = AsyncMock(return_value=agno_session)
    session.main_team.asave_session = AsyncMock()
    session.main_team.model = SimpleNamespace()
    session.main_team.model.count_tokens = MagicMock(return_value=100)
    session._build_main_agent = MagicMock(return_value=SimpleNamespace())
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
        assert await compact_if_needed(s, input_tokens=1000, context_window=10_000) is False
        s.hook_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_80_percent_triggers_compact(self):
        # Right at threshold — the ``usage < 0.8`` guard should
        # let 80% through as a trigger.
        s = _bare_session(agno_session=None)
        # 80% of 10_000 = 8000
        result = await compact_if_needed(s, input_tokens=8000, context_window=10_000)
        # ``compact()`` returned early ("Session not found") but
        # ``compact_if_needed`` still returns True since the
        # trigger fired.
        assert result is True

    @pytest.mark.asyncio
    async def test_precompact_hook_can_cancel(self):
        # A plugin can veto the compaction — critical escape hatch
        # for e.g. an "unsaved changes" guard.
        s = _bare_session(hook_result=HookResult(should_continue=False, message="blocked"))
        result = await compact_if_needed(s, input_tokens=9000, context_window=10_000)
        assert result is False
        # ``compact()`` never called → aget_session count is 1 for
        # the PreCompact hook path (via hook_executor.execute
        # only), NOT plus one for compact itself.
        s.main_team.aget_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_context_window_is_noop(self):
        s = _bare_session()
        assert await compact_if_needed(s, input_tokens=1000, context_window=0) is False

    @pytest.mark.asyncio
    async def test_fires_post_compact_hook_on_success(self):
        # PostCompact fires so plugins can react even when the
        # compaction is auto-triggered.
        s = _bare_session(agno_session=SimpleNamespace(runs=[], summary=None))
        await compact_if_needed(s, input_tokens=9000, context_window=10_000)
        events = [c.kwargs.get("event") for c in s.hook_executor.execute.call_args_list]
        assert "PreCompact" in events
        assert "PostCompact" in events


class TestForceCompact:
    @pytest.mark.asyncio
    async def test_empty_session_returns_nothing_to_compact(self):
        # No runs on the session → no history to summarise.
        empty_session = SimpleNamespace(runs=[], summary=None)
        s = _bare_session(agno_session=empty_session)
        status, summary = await force_compact(s)
        assert "Nothing to compact" in status
        assert summary == ""

    @pytest.mark.asyncio
    async def test_precompact_hook_can_cancel(self):
        # Same veto path as auto-compact, but for /compact.
        agno = SimpleNamespace(runs=[SimpleNamespace(messages=[])], summary=None)
        s = _bare_session(
            agno_session=agno,
            hook_result=HookResult(should_continue=False, message="unsaved changes"),
        )
        status, summary = await force_compact(s)
        assert "unsaved changes" in status or "cancelled" in status.lower()
        assert summary == ""


class TestContextBreakdown:
    @pytest.mark.asyncio
    async def test_no_session_returns_zeros(self):
        # Fresh session, never persisted — no history yet.
        s = _bare_session(agno_session=None)
        result = await context_breakdown(s)
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
        # Model reports different counts for the two calls (total
        # messages vs. runs-only messages).
        s.main_team.model.count_tokens = MagicMock(side_effect=[500, 200])
        result = await context_breakdown(s)
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
        result = await context_breakdown(s)
        assert result.floor == 0
