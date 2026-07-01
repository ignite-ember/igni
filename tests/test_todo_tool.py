"""Tests for ``TodoWrite`` — CC's planning tool, ember parity.

Covers:
- ``TodoStore`` set/snapshot
- ``_coerce_items`` validation (status enum, required content,
  camelCase ↔ snake_case ``activeForm`` aliasing)
- ``TodoTools.todo_write`` semantics (atomic replace, summary
  string, multi-in_progress warning, validation error surfacing)
- The ``GET_TODOS`` RPC endpoint
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ember_code.backend.server import BackendServer
from ember_code.core.tools.todo import (
    TodoItem,
    TodoStore,
    TodoTools,
    _coerce_items,
)

# ── Pure data layer ───────────────────────────────────────────


class TestTodoStore:
    def test_set_replaces_atomically(self):
        store = TodoStore()
        store.set([TodoItem("a", "pending"), TodoItem("b", "completed")])
        store.set([TodoItem("c", "in_progress", "Doing c")])
        # Old items are gone — no merge.
        assert len(store.items) == 1
        assert store.items[0].content == "c"

    def test_snapshot_uses_camelcase_active_form(self):
        store = TodoStore()
        store.set([TodoItem("Run tests", "in_progress", "Running tests")])
        snap = store.snapshot()
        assert snap == [
            {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"}
        ]

    def test_empty_store_snapshots_to_empty_list(self):
        assert TodoStore().snapshot() == []


# ── Validation ────────────────────────────────────────────────


class TestCoerceItems:
    def test_valid_entries_pass(self):
        items, errs = _coerce_items(
            [
                {"content": "a", "status": "pending"},
                {"content": "b", "status": "in_progress", "activeForm": "Doing b"},
                {"content": "c", "status": "completed"},
            ]
        )
        assert errs == []
        assert [i.content for i in items] == ["a", "b", "c"]
        assert items[1].active_form == "Doing b"

    def test_non_list_input(self):
        items, errs = _coerce_items({"not": "a list"})
        assert items == []
        assert any("list" in e for e in errs)

    def test_empty_content_rejected(self):
        items, errs = _coerce_items([{"content": "  ", "status": "pending"}])
        assert items == []
        assert any("empty content" in e for e in errs)

    def test_unknown_status_rejected(self):
        items, errs = _coerce_items([{"content": "a", "status": "wat"}])
        assert items == []
        assert any("status" in e and "wat" in e for e in errs)

    def test_non_dict_row_rejected(self):
        items, errs = _coerce_items(["not a dict", {"content": "a", "status": "pending"}])
        # Bad row dropped, good row kept.
        assert [i.content for i in items] == ["a"]
        assert any("not a dict" in e for e in errs)

    def test_snake_case_active_form_accepted(self):
        """The agent might output either ``activeForm`` (CC
        convention) or ``active_form`` (Python-natural). Accept
        both so the model doesn't trip on the casing."""
        items, _ = _coerce_items(
            [{"content": "a", "status": "in_progress", "active_form": "Doing a"}]
        )
        assert items[0].active_form == "Doing a"

    def test_status_is_normalized_lowercase(self):
        items, errs = _coerce_items([{"content": "a", "status": "IN_PROGRESS"}])
        assert errs == []
        assert items[0].status == "in_progress"


# ── Tool behavior ─────────────────────────────────────────────


def _make_session():
    session = MagicMock()
    session.todo_store = TodoStore()
    return session


class TestTodoWrite:
    @pytest.mark.asyncio
    async def test_replaces_list_on_each_call(self):
        session = _make_session()
        tool = TodoTools(session)
        await tool.todo_write([{"content": "a", "status": "pending"}])
        await tool.todo_write([{"content": "b", "status": "in_progress"}])
        assert [i.content for i in session.todo_store.items] == ["b"]

    @pytest.mark.asyncio
    async def test_summary_counts_each_status(self):
        session = _make_session()
        tool = TodoTools(session)
        result = await tool.todo_write(
            [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "in_progress"},
                {"content": "c", "status": "pending"},
                {"content": "d", "status": "pending"},
            ]
        )
        assert "4 todos" in result
        assert "1 completed" in result
        assert "1 in_progress" in result
        assert "2 pending" in result

    @pytest.mark.asyncio
    async def test_empty_list_clears(self):
        session = _make_session()
        session.todo_store.set([TodoItem("old", "pending")])
        tool = TodoTools(session)
        result = await tool.todo_write([])
        assert "Cleared" in result
        assert session.todo_store.items == []

    @pytest.mark.asyncio
    async def test_multi_in_progress_warns_in_summary(self):
        """The "at most one in_progress" rule is encouraged via
        the reply string — the model sees the warning and self-
        corrects on the next call."""
        session = _make_session()
        tool = TodoTools(session)
        result = await tool.todo_write(
            [
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ]
        )
        assert "at most one" in result.lower()

    @pytest.mark.asyncio
    async def test_validation_errors_surfaced(self):
        """Bad rows are dropped (so the rest of the list still
        applies) and the errors come back in the reply so the
        agent can correct."""
        session = _make_session()
        tool = TodoTools(session)
        result = await tool.todo_write(
            [
                {"content": "a", "status": "pending"},
                {"content": "", "status": "pending"},  # bad: empty content
            ]
        )
        assert "Validation errors" in result
        assert "empty content" in result
        # Good row still applied.
        assert len(session.todo_store.items) == 1


# ── RPC ───────────────────────────────────────────────────────


class TestGetTodosRpc:
    def test_returns_snapshot(self, tmp_path):
        session = MagicMock()
        session.todo_store = TodoStore()
        session.todo_store.set([TodoItem("Run tests", "in_progress", "Running tests")])
        backend = BackendServer.__new__(BackendServer)
        backend._session = session
        out = backend.get_todos()
        assert out == [
            {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"}
        ]

    def test_returns_empty_when_no_list(self):
        """If the agent never called ``todo_write``, the store
        exists but is empty — RPC returns ``[]`` cleanly."""
        session = MagicMock()
        session.todo_store = TodoStore()
        backend = BackendServer.__new__(BackendServer)
        backend._session = session
        assert backend.get_todos() == []

    def test_returns_empty_when_store_missing(self):
        """Defensive: if a Session was somehow constructed
        without a ``todo_store`` attribute (legacy serialised
        session, partial init), the RPC mustn't crash."""
        session = MagicMock(spec=[])  # No attributes at all.
        backend = BackendServer.__new__(BackendServer)
        backend._session = session
        assert backend.get_todos() == []

    @pytest.mark.asyncio
    async def test_dispatch_table_routes_get_todos(self):
        from ember_code.backend.__main__ import _build_rpc_table
        from ember_code.protocol.rpc import RpcMethod

        session = MagicMock()
        session.todo_store = TodoStore()
        session.todo_store.set([TodoItem("planned", "pending")])
        backend = BackendServer.__new__(BackendServer)
        backend._session = session

        table = _build_rpc_table(backend, transport=MagicMock(), login_state={})
        handler = table.get(RpcMethod.GET_TODOS)
        assert handler is not None
        result = handler({})
        assert isinstance(result, list)
        assert result[0]["content"] == "planned"


# ── Integration ─────────────────────────────────────────────


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_agent_loop_visible_via_rpc(self):
        """Simulate the realistic flow: agent calls todo_write
        twice (initial plan, then progresses one item), and the
        RPC at each step reflects exactly what was last written."""
        session = MagicMock()
        session.todo_store = TodoStore()
        tool = TodoTools(session)
        backend = BackendServer.__new__(BackendServer)
        backend._session = session

        # First call: plan three items.
        await tool.todo_write(
            [
                {"content": "Read spec", "status": "in_progress", "activeForm": "Reading spec"},
                {"content": "Write code", "status": "pending"},
                {"content": "Run tests", "status": "pending"},
            ]
        )
        snap = backend.get_todos()
        statuses = [e["status"] for e in snap]
        assert statuses == ["in_progress", "pending", "pending"]

        # Second call: mark step 1 done, step 2 in progress.
        await tool.todo_write(
            [
                {"content": "Read spec", "status": "completed"},
                {"content": "Write code", "status": "in_progress", "activeForm": "Writing code"},
                {"content": "Run tests", "status": "pending"},
            ]
        )
        snap = backend.get_todos()
        statuses = [e["status"] for e in snap]
        assert statuses == ["completed", "in_progress", "pending"]
        # Active form for the active step is what the UI renders.
        assert snap[1]["activeForm"] == "Writing code"
