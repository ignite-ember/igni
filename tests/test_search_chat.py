"""Tests for the ``search_chat`` RPC — case-insensitive substring
scan across persisted session history, with snippet + match-offset
output the FE highlight logic relies on.

Two layers:

* ``_search_history`` — pure function over a chat-history list,
  trivially testable without Agno. The bulk of the contract lives
  here (snippet truncation, ellipses, match offsets, limit cap,
  defensive skips for non-string content).
* ``BackendServer.search_chat`` — thin wrapper that strips the
  query, fetches history, delegates. Tests verify the strip +
  delegation + empty-query short-circuit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.server import (
    _SEARCH_CHAT_SNIPPET_HALF_WIDTH,
    BackendServer,
    _search_history,
)


def _turn(
    content: str,
    *,
    role: str = "user",
    run_id: str = "r1",
    created_at: int = 100,
) -> dict:
    """One chat history turn in the shape ``get_chat_history``
    emits."""
    return {
        "content": content,
        "role": role,
        "run_id": run_id,
        "created_at": created_at,
    }


# ── _search_history ─────────────────────────────────────────


class TestSearchHistoryEdges:
    def test_empty_needle_returns_empty(self):
        """``str.find("")`` returns 0 for every string — without
        the explicit early-return we'd emit a match for every
        turn. Defense in depth (the caller already strips, but
        an empty needle slipping through must not flood the
        results)."""
        history = [_turn("hello"), _turn("world")]
        assert _search_history(history, "", 50) == []

    def test_no_match_returns_empty(self):
        history = [_turn("hello"), _turn("world")]
        assert _search_history(history, "zzz", 50) == []

    def test_skips_non_string_content(self):
        """Tool turns sometimes carry dict/list content. Skipping
        them silently is the right call — the user is searching
        for prose, not tool payload."""
        history = [
            {"content": {"key": "val"}, "role": "tool", "run_id": "r"},
            {"content": ["a", "b"], "role": "tool", "run_id": "r"},
            _turn("real prose with NEEDLE inside"),
        ]
        out = _search_history(history, "needle", 50)
        assert len(out) == 1
        assert out[0]["history_index"] == 2

    def test_skips_empty_string_content(self):
        history = [_turn(""), _turn("has NEEDLE in it")]
        out = _search_history(history, "needle", 50)
        assert len(out) == 1
        assert out[0]["history_index"] == 1

    def test_history_index_matches_input_position(self):
        """``history_index`` must align with ``get_chat_history``'s
        emission order — the FE keeps a parallel
        ``historyIndex → itemIndex`` map built at session load,
        so any drift breaks the "click result → jump to chat
        item" mapping."""
        history = [
            _turn("no match here"),
            _turn("no match"),
            _turn("the target word lives here"),
            _turn("nothing"),
        ]
        out = _search_history(history, "target", 50)
        assert len(out) == 1
        assert out[0]["history_index"] == 2


class TestSearchHistorySnippet:
    def test_match_at_start_no_leading_ellipsis(self):
        history = [_turn("NEEDLE at the start of this string")]
        out = _search_history(history, "needle", 50)
        assert out[0]["snippet"].startswith("NEEDLE")
        # Leading ellipsis is absent — match was at the start.
        assert not out[0]["snippet"].startswith("…")

    def test_match_at_end_no_trailing_ellipsis(self):
        history = [_turn("the end of the line is NEEDLE")]
        out = _search_history(history, "needle", 50)
        assert out[0]["snippet"].endswith("NEEDLE")
        assert not out[0]["snippet"].endswith("…")

    def test_long_content_truncated_with_both_ellipses(self):
        """Content longer than 2 * SNIPPET_HALF_WIDTH around the
        match → both leading and trailing ellipsis."""
        prefix = "x" * 200
        suffix = "y" * 200
        history = [_turn(f"{prefix} NEEDLE {suffix}")]
        out = _search_history(history, "needle", 50)
        snip = out[0]["snippet"]
        assert snip.startswith("…")
        assert snip.endswith("…")
        # Snippet is bounded: 2 * half_width + len(needle) + 2 ellipsis chars.
        assert len(snip) <= 2 * _SEARCH_CHAT_SNIPPET_HALF_WIDTH + len("NEEDLE") + 2

    def test_match_offsets_are_relative_to_snippet(self):
        """The FE highlights by slicing snippet[match_start:match_end].
        Offsets must be relative to the SNIPPET string, not the
        full content — otherwise the highlight lands on the
        wrong characters."""
        prefix = "x" * 200
        history = [_turn(f"{prefix} NEEDLE more")]
        out = _search_history(history, "needle", 50)
        snip = out[0]["snippet"]
        # Slice with the offsets — must return the match text.
        sliced = snip[out[0]["match_start"] : out[0]["match_end"]]
        assert sliced.lower() == "needle"

    def test_match_offsets_account_for_leading_ellipsis(self):
        """When a leading ellipsis is added, the match shifts
        forward by 1 in the snippet. The bookkeeping must
        include that shift."""
        prefix = "x" * 200
        history = [_turn(f"{prefix} NEEDLE")]
        out = _search_history(history, "needle", 50)
        snip = out[0]["snippet"]
        # First char of the snippet must be the ellipsis.
        assert snip[0] == "…"
        # And the slice still extracts the match.
        assert snip[out[0]["match_start"] : out[0]["match_end"]].lower() == "needle"


class TestSearchHistoryCaseAndLimit:
    def test_case_insensitive_match(self):
        """``find`` is called on lowercased content + lowercased
        needle. NEEDLE / Needle / neeDLE all match the same
        position."""
        history = [_turn("contains MiXeD case Needle inside")]
        out = _search_history(history, "NEEDLE", 50)
        assert len(out) == 1
        # Snippet preserves original case.
        assert "Needle" in out[0]["snippet"]

    def test_limit_caps_results(self):
        """Once we've collected ``limit`` matches we stop scanning
        — guards against pathological queries against megabyte-
        sized history."""
        history = [_turn(f"match {i} NEEDLE") for i in range(20)]
        out = _search_history(history, "needle", limit=5)
        assert len(out) == 5
        # And we stopped at the first 5, not skipped any.
        assert [m["history_index"] for m in out] == [0, 1, 2, 3, 4]

    def test_only_first_match_per_turn_captured(self):
        """``find`` returns the FIRST occurrence — the function
        doesn't enumerate every occurrence within a single turn.
        That's the documented behavior (one match per turn
        keeps the result list scannable)."""
        history = [_turn("NEEDLE first NEEDLE second NEEDLE third")]
        out = _search_history(history, "needle", 50)
        assert len(out) == 1


class TestSearchHistoryFieldDefaults:
    def test_missing_role_defaults_to_empty(self):
        history = [{"content": "with NEEDLE inside"}]
        out = _search_history(history, "needle", 50)
        assert out[0]["role"] == ""

    def test_missing_run_id_defaults_to_empty(self):
        history = [{"content": "with NEEDLE inside"}]
        out = _search_history(history, "needle", 50)
        assert out[0]["run_id"] == ""

    def test_missing_created_at_defaults_to_zero(self):
        """The FE renders epoch 0 as "long ago" / a generic
        timestamp, which is the right fallback when the BE
        didn't include the field. Numeric, never None — the FE
        would crash on a None timestamp."""
        history = [{"content": "with NEEDLE inside"}]
        out = _search_history(history, "needle", 50)
        assert out[0]["created_at"] == 0
        assert isinstance(out[0]["created_at"], int)


# ── BackendServer.search_chat wrapper ──────────────────────


class TestSearchChatWrapper:
    def _backend(self, history: list[dict]):
        backend = BackendServer.__new__(BackendServer)
        backend.get_chat_history = AsyncMock(return_value=history)
        backend._session = MagicMock()
        return backend

    @pytest.mark.asyncio
    async def test_empty_query_short_circuits(self):
        """Don't even hit the DB if the query strips to empty.
        Cheap optimisation for the common case where the user
        types a space + immediately deletes it."""
        backend = self._backend([_turn("ignored")])
        out = await backend.search_chat("s1", "")
        assert out == []
        backend.get_chat_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitespace_only_query_short_circuits(self):
        backend = self._backend([_turn("ignored")])
        out = await backend.search_chat("s1", "   \t\n  ")
        assert out == []
        backend.get_chat_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_query_stripped_before_search(self):
        """``  needle  `` should match the same content as
        ``needle`` — leading/trailing whitespace from a hurried
        user shouldn't break the search."""
        backend = self._backend([_turn("contains NEEDLE here")])
        out = await backend.search_chat("s1", "  needle  ")
        assert len(out) == 1

    @pytest.mark.asyncio
    async def test_delegates_to_get_chat_history(self):
        backend = self._backend([_turn("NEEDLE here")])
        await backend.search_chat("abc123", "needle")
        backend.get_chat_history.assert_awaited_once_with("abc123")

    @pytest.mark.asyncio
    async def test_limit_propagates(self):
        backend = self._backend([_turn(f"NEEDLE {i}") for i in range(20)])
        out = await backend.search_chat("s1", "needle", limit=3)
        assert len(out) == 3

    @pytest.mark.asyncio
    async def test_default_limit_is_50(self):
        backend = self._backend([_turn(f"NEEDLE {i}") for i in range(100)])
        out = await backend.search_chat("s1", "needle")
        assert len(out) == 50

    @pytest.mark.asyncio
    async def test_dispatch_table_routes_search_chat(self):
        """Wiring check: ``RpcMethod.SEARCH_CHAT`` resolves to the
        backend's method through the actual dispatch table."""
        from ember_code.backend.__main__ import _build_rpc_table
        from ember_code.protocol.rpc import RpcMethod

        backend = self._backend([_turn("foo NEEDLE bar")])
        table = _build_rpc_table(backend, transport=MagicMock(), login_state={})
        handler = table.get(RpcMethod.SEARCH_CHAT)
        assert handler is not None
        # The handler is an async lambda calling ``backend.search_chat``;
        # awaiting it gives back the match list.
        out = await handler({"session_id": "s1", "query": "needle", "limit": 50})
        assert len(out) == 1
