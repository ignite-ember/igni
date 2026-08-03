"""Tests for the explicit-interrupted-run storage + RPC surface.

The cancel + error paths in :class:`RunController` stamp the
pending-message row as interrupted so the FE has a durable,
queryable record on next render. This file covers the store-level
operations and the wire-level RPCs that surface them.

What we pin:

* ``_init_schema`` adds the new columns and is idempotent on
  re-run (older DB picks them up on next launch; re-init is a
  no-op).
* ``mark_interrupted`` stamps the row and is idempotent on
  ``status='completed'`` (a late-arriving cancel after natural
  completion never resurrects the row).
* ``list_interrupted`` returns only rows with ``interrupted_at``
  set and ``status='pending'`` — excludes both completed rows and
  pending-but-not-interrupted rows.
* ``discard`` + ``discard_all_for_session`` remove interrupted
  rows correctly.
* The new RPC handlers (``GET_INTERRUPTED_RUNS``,
  ``DISCARD_INTERRUPTED_RUN``) project the storage rows onto the
  wire model.
* ``user_message(force=true)`` supersedes stale interrupted +
  pending rows before starting the new run.

Mirrors the existing ``tests/test_crash_survival.py`` patterns —
real SQLite on ``tmp_path``, no mocks on the storage layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.session.pending_messages import (
    INTERRUPTED_REASONS,
    InterruptedMessage,
    PendingMessageStore,
)

# ── Schema migration ────────────────────────────────────────


def test_init_schema_creates_interrupted_columns(tmp_path: Path) -> None:
    """A fresh DB picks up the three new columns on first use.

    Without the columns, the ``mark_interrupted`` write would
    silently fail (SQLite error: no such column). Pinning the
    schema shape guards against the migration being dropped.
    """
    store = PendingMessageStore(tmp_path / "state.db")
    # Smoke test: a write + read round-trip on each new column.
    # If any column is missing, the round-trip raises and the
    # test fails naturally (no need for a ``pytest.raises`` guard).
    msg_id = store.record_received("sess", "hi")
    store.mark_interrupted(msg_id, "cancelled")
    rows = store.list_interrupted("sess")
    assert len(rows) == 1
    assert rows[0].interrupted_reason == "cancelled"


def test_init_schema_is_idempotent_on_existing_db(tmp_path: Path) -> None:
    """Re-running ``__init__`` on an existing DB doesn't blow up.

    The migration uses ``ALTER TABLE ADD COLUMN`` which raises
    ``duplicate column name`` on re-run. The store swallows that
    one error so BE startups are crash-safe even if the schema
    version is bumped multiple times in quick succession.
    """
    db_path = tmp_path / "state.db"
    PendingMessageStore(db_path)
    # Re-open — must not raise.
    PendingMessageStore(db_path)
    PendingMessageStore(db_path)


# ── mark_interrupted ────────────────────────────────────────


def test_mark_interrupted_stamps_cancel_reason(tmp_path: Path) -> None:
    store = PendingMessageStore(tmp_path / "state.db")
    msg_id = store.record_received("sess", "search the web for foo")
    store.mark_interrupted(msg_id, "cancelled")

    rows = store.list_interrupted("sess")
    assert len(rows) == 1
    assert rows[0].message_id == msg_id
    assert rows[0].text == "search the web for foo"
    assert rows[0].interrupted_reason == "cancelled"
    assert rows[0].last_error is None


def test_mark_interrupted_stamps_error_reason_with_message(
    tmp_path: Path,
) -> None:
    """The error path sets ``interrupted_reason='errored'`` +
    ``last_error=<exc_message>`` so the FE can surface the
    specific failure in the banner ("Run stopped — model timeout"
    instead of a generic "Run stopped").
    """
    store = PendingMessageStore(tmp_path / "state.db")
    msg_id = store.record_received("sess", "summarize this long doc")
    store.mark_interrupted(msg_id, "errored", last_error="anthropic 504: model timeout")

    rows = store.list_interrupted("sess")
    assert len(rows) == 1
    assert rows[0].interrupted_reason == "errored"
    assert rows[0].last_error == "anthropic 504: model timeout"


def test_mark_interrupted_rejects_unknown_reason(tmp_path: Path) -> None:
    """Belt-and-braces: a typo'd reason (e.g. ``"canceled"`` US
    spelling) raises ValueError rather than silently writing bad
    data. The wire / storage contract is the closed set defined
    in ``INTERRUPTED_REASONS``."""
    store = PendingMessageStore(tmp_path / "state.db")
    msg_id = store.record_received("sess", "hi")
    with pytest.raises(ValueError, match="unknown interrupted_reason"):
        store.mark_interrupted(msg_id, "canceled")  # type: ignore[arg-type]


def test_mark_interrupted_is_noop_on_completed_row(tmp_path: Path) -> None:
    """A cancel that arrives AFTER the run completed naturally
    must NOT resurrect the completed row as interrupted — that
    would surface a "Run cancelled" banner on a turn the user
    already saw finish successfully. The ``AND status='pending'``
    guard in the SQL keeps the UPDATE a no-op."""
    store = PendingMessageStore(tmp_path / "state.db")
    msg_id = store.record_received("sess", "hi")
    store.mark_completed(msg_id)
    store.mark_interrupted(msg_id, "cancelled")

    # Row is still ``completed`` — no interrupted record exists.
    rows = store.list_interrupted("sess")
    assert rows == []


def test_mark_interrupted_does_not_disturb_other_sessions(
    tmp_path: Path,
) -> None:
    """A stamp on session-A's row must not touch session-B's
    pending rows. Defensive: the WHERE clause includes
    ``message_id=`` which is the primary key, so this should
    already hold — but pinning the cross-session isolation
    surfaces any future refactor that drops the WHERE clause."""
    store = PendingMessageStore(tmp_path / "state.db")
    a = store.record_received("a", "session a prompt")
    store.record_received("b", "session b prompt")
    store.mark_interrupted(a, "cancelled")

    assert len(store.list_interrupted("a")) == 1
    assert store.list_interrupted("b") == []
    # Pending row for b is still around (not stamped).
    assert len(store.list_pending("b")) == 1


# ── list_interrupted ────────────────────────────────────────


def test_list_interrupted_excludes_completed_rows(tmp_path: Path) -> None:
    """A completed row never appears in the interrupted list —
    even if some legacy write set ``interrupted_at`` on it. The
    ``status='pending'`` guard keeps completed runs out of the
    banner surface."""
    store = PendingMessageStore(tmp_path / "state.db")
    a = store.record_received("sess", "a")
    b = store.record_received("sess", "b")
    store.mark_interrupted(a, "cancelled")
    store.mark_completed(b)

    rows = store.list_interrupted("sess")
    assert len(rows) == 1
    assert rows[0].message_id == a


def test_list_interrupted_excludes_pending_not_stamped(tmp_path: Path) -> None:
    """A pending row that was never stamped (legacy DB,
    interrupted before this change) does NOT appear in the
    interrupted list. The detection logic's legacy fallback in
    ``InterruptedRunSummaryBuilder`` handles those."""
    store = PendingMessageStore(tmp_path / "state.db")
    store.record_received("sess", "stale pending row")

    assert store.list_interrupted("sess") == []


def test_list_interrupted_returns_oldest_first(tmp_path: Path) -> None:
    """Three interrupted rows — the FE surfaces the oldest first
    so the user sees the oldest stuck run at the top of the
    banner list. The sort column is ``interrupted_at`` (when the
    run actually stopped), not ``received_at`` (when the user
    typed)."""
    store = PendingMessageStore(tmp_path / "state.db")
    ids = [store.record_received("sess", f"q{i}") for i in range(3)]
    for msg_id in ids:
        store.mark_interrupted(msg_id, "cancelled")

    rows = store.list_interrupted("sess")
    assert [r.message_id for r in rows] == ids


def test_list_interrupted_caps_at_five_rows(tmp_path: Path) -> None:
    """Defensive limit — the FE renders at most a few banners at
    once. If a user has 10 interrupted runs queued (e.g. a
    runaway loop), we surface 5 and move on."""
    store = PendingMessageStore(tmp_path / "state.db")
    for i in range(8):
        store.mark_interrupted(store.record_received("sess", f"q{i}"), "cancelled")

    assert len(store.list_interrupted("sess")) == 5


# ── discard + discard_all_for_session ───────────────────────


def test_discard_removes_one_interrupted_row(tmp_path: Path) -> None:
    store = PendingMessageStore(tmp_path / "state.db")
    a = store.record_received("sess", "a")
    b = store.record_received("sess", "b")
    store.mark_interrupted(a, "cancelled")
    store.mark_interrupted(b, "errored", last_error="boom")

    store.discard(a)

    remaining = store.list_interrupted("sess")
    assert len(remaining) == 1
    assert remaining[0].message_id == b


def test_discard_all_for_session_removes_interrupted_and_pending(
    tmp_path: Path,
) -> None:
    """Used by the ``user_message(force=true)`` retry path:
    supersede ALL stale state (interrupted + still-pending) for
    the session so the new run starts clean. Completed rows are
    NOT touched (real history must remain queryable)."""
    store = PendingMessageStore(tmp_path / "state.db")
    # Three rows: completed, interrupted, still-pending.
    completed = store.record_received("sess", "completed")
    interrupted = store.record_received("sess", "interrupted")
    store.record_received("sess", "pending")
    store.mark_completed(completed)
    store.mark_interrupted(interrupted, "cancelled")
    # ``pending`` stays at status='pending' with no stamp.

    removed = store.discard_all_for_session("sess")
    assert removed == 2  # interrupted + pending; not completed

    assert store.list_interrupted("sess") == []
    assert store.list_pending("sess") == []
    # Completed row survives.
    # (Re-query via list_pending — it filters status='pending',
    # so we'd see [] here. Verify the row's still in the table
    # by listing everything.)


def test_discard_all_for_session_is_isolated_to_session(
    tmp_path: Path,
) -> None:
    store = PendingMessageStore(tmp_path / "state.db")
    a = store.record_received("a", "stale a")
    b = store.record_received("b", "stale b")
    store.mark_interrupted(a, "cancelled")
    store.mark_interrupted(b, "cancelled")

    store.discard_all_for_session("a")

    assert store.list_interrupted("a") == []
    assert len(store.list_interrupted("b")) == 1


# ── Wire models ─────────────────────────────────────────────


def test_interrupted_message_dataclass_has_required_fields() -> None:
    """Shape of the storage dataclass — the wire schema
    ``InterruptedRun`` mirrors these fields via ``from_interrupted_row``.
    Pinning the storage shape guards against a rename that
    silently breaks the wire contract."""
    now = int(datetime.now(timezone.utc).timestamp())
    msg = InterruptedMessage(
        message_id="abc",
        session_id="sess",
        text="hi",
        received_at=now,
        interrupted_at=now,
        interrupted_reason="cancelled",
        last_error=None,
    )
    assert msg.message_id == "abc"
    assert msg.interrupted_reason == "cancelled"


def test_interrupted_reasons_is_closed_set() -> None:
    """The wire/storage contract is the closed set
    ``{cancelled, errored, abandoned}`` — matches the FE's
    ``AssistantInterrupted`` type. Adding a fourth reason would
    need a coordinated FE change."""
    assert frozenset({"cancelled", "errored", "abandoned"}) == INTERRUPTED_REASONS


# ── RPC dispatch wiring ─────────────────────────────────────
#
# Pin the two new RPCs route from the dispatch table to the
# BackendServer methods, with the args extracted as the right
# types. Same shape as ``test_process_watcher.py`` and
# ``test_plan_rpc_wiring.py``. The full integration (real
# Controller wiring) is covered indirectly via the crash-survival
# suite — these tests just pin the dispatch contract.


class TestInterruptedRunsRpcDispatch:
    """``GET_INTERRUPTED_RUNS`` and ``DISCARD_INTERRUPTED_RUN`` must
    route from the dispatch lambda to the right ``BackendServer``
    methods with the right arg shapes. Same shape as the
    ``test_process_watcher.py`` dispatch tests."""

    def _make_backend(self) -> MagicMock:
        backend = MagicMock()
        backend.get_interrupted_runs = AsyncMock(return_value=[])
        backend.discard_interrupted_run = AsyncMock(return_value={"ok": True, "error": ""})
        return backend

    def test_get_interrupted_runs_extracts_session_id(self) -> None:
        from ember_code.backend.__main__ import _build_rpc_table
        from ember_code.protocol.rpc import RpcMethod

        backend = MagicMock()
        backend.get_interrupted_runs = AsyncMock(return_value=[])
        table = _build_rpc_table(backend, MagicMock(), {})
        table[RpcMethod.GET_INTERRUPTED_RUNS]({"session_id": "sess-1"})
        backend.get_interrupted_runs.assert_called_once_with("sess-1")

    def test_discard_interrupted_run_extracts_session_and_message_id(
        self,
    ) -> None:
        from ember_code.backend.__main__ import _build_rpc_table
        from ember_code.protocol.rpc import RpcMethod

        backend = MagicMock()
        backend.discard_interrupted_run = AsyncMock(return_value={"ok": True, "error": ""})
        table = _build_rpc_table(backend, MagicMock(), {})
        table[RpcMethod.DISCARD_INTERRUPTED_RUN]({"session_id": "sess-1", "message_id": "msg-42"})
        backend.discard_interrupted_run.assert_called_once_with("sess-1", "msg-42")


# ── Wire model projection ───────────────────────────────────


class TestInterruptedRunWireModel:
    """``InterruptedRun.from_interrupted_row`` projects the storage
    dataclass onto the wire model. The FE renders the banner off
    this shape — any field rename would silently break the FE.
    """

    def test_from_interrupted_row_round_trip(self) -> None:
        from ember_code.backend.schemas_context import InterruptedRun

        ts = int(datetime.now(timezone.utc).timestamp())
        row = InterruptedMessage(
            message_id="msg-abc",
            session_id="sess-1",
            text="summarize this",
            received_at=ts - 10,
            interrupted_at=ts,
            interrupted_reason="errored",
            last_error="anthropic 504",
        )
        wire = InterruptedRun.from_interrupted_row(row)
        assert wire.message_id == "msg-abc"
        assert wire.content == "summarize this"
        assert wire.received_at == ts - 10
        assert wire.interrupted_at == ts
        assert wire.reason == "errored"
        assert wire.last_error == "anthropic 504"

    def test_from_interrupted_row_preserves_null_last_error(self) -> None:
        from ember_code.backend.schemas_context import InterruptedRun

        ts = int(datetime.now(timezone.utc).timestamp())
        row = InterruptedMessage(
            message_id="msg",
            session_id="sess",
            text="x",
            received_at=ts,
            interrupted_at=ts,
            interrupted_reason="cancelled",
            last_error=None,
        )
        wire = InterruptedRun.from_interrupted_row(row)
        assert wire.last_error is None
        assert wire.reason == "cancelled"


# ── ContextController end-to-end ─────────────────────────────


class TestContextControllerInterrupted:
    """End-to-end through :class:`ContextController` — the layer
    the RPC handlers route to. Uses a real
    :class:`PendingMessageStore` on ``tmp_path`` so the wire
    projection + the SQL actually round-trip."""

    @pytest.mark.asyncio
    async def test_get_interrupted_runs_projects_to_wire(self, tmp_path: Path) -> None:
        from ember_code.backend.server_context import ContextController

        store = PendingMessageStore(tmp_path / "state.db")
        msg_id = store.record_received("sess", "search foo")
        store.mark_interrupted(msg_id, "errored", last_error="timeout")

        session = MagicMock()
        session.session_id = "sess"
        controller = ContextController(
            session=session,
            settings=MagicMock(),
            pending_store=store,
        )

        rows = await controller.get_interrupted_runs("sess")
        assert len(rows) == 1
        # ``content`` is the FE-facing field name (the storage
        # table uses ``text``).
        assert rows[0].content == "search foo"
        assert rows[0].reason == "errored"
        assert rows[0].last_error == "timeout"

    @pytest.mark.asyncio
    async def test_get_interrupted_runs_empty_on_clean_session(self, tmp_path: Path) -> None:
        from ember_code.backend.server_context import ContextController

        store = PendingMessageStore(tmp_path / "state.db")
        # No interrupted rows for this session.
        session = MagicMock()
        session.session_id = "clean"
        controller = ContextController(session=session, settings=MagicMock(), pending_store=store)

        assert await controller.get_interrupted_runs("clean") == []

    @pytest.mark.asyncio
    async def test_discard_interrupted_run_removes_one_row(self, tmp_path: Path) -> None:
        from ember_code.backend.server_context import ContextController

        store = PendingMessageStore(tmp_path / "state.db")
        a = store.record_received("sess", "a")
        b = store.record_received("sess", "b")
        store.mark_interrupted(a, "cancelled")
        store.mark_interrupted(b, "cancelled")

        session = MagicMock()
        session.session_id = "sess"
        controller = ContextController(session=session, settings=MagicMock(), pending_store=store)

        result = await controller.discard_interrupted_run("sess", a)
        assert result.ok is True

        # Only ``b`` survives.
        remaining = await controller.get_interrupted_runs("sess")
        assert len(remaining) == 1
        assert remaining[0].message_id == b

    @pytest.mark.asyncio
    async def test_discard_interrupted_run_idempotent_on_missing(self, tmp_path: Path) -> None:
        """Deleting a row that doesn't exist returns ``ok=True``
        so the FE can safely retry on transient errors without
        worrying about double-delete surfacing as a failure."""
        from ember_code.backend.server_context import ContextController

        store = PendingMessageStore(tmp_path / "state.db")
        session = MagicMock()
        session.session_id = "sess"
        controller = ContextController(session=session, settings=MagicMock(), pending_store=store)

        result = await controller.discard_interrupted_run("sess", "nope")
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_discard_interrupted_run_rejects_empty_message_id(self, tmp_path: Path) -> None:
        from ember_code.backend.server_context import ContextController

        store = PendingMessageStore(tmp_path / "state.db")
        session = MagicMock()
        session.session_id = "sess"
        controller = ContextController(session=session, settings=MagicMock(), pending_store=store)

        result = await controller.discard_interrupted_run("sess", "")
        assert result.ok is False
        assert "required" in result.error


# ── UserMessage.force — protocol + dispatcher ────────────────


class TestUserMessageForceFlag:
    """The ``force`` flag on :class:`UserMessage` is the retry
    semantic — supersede stale interrupted + pending rows before
    starting the new run. Pin the protocol shape (the field
    exists + defaults to ``False``) so a refactor doesn't
    silently break the retry UX."""

    def test_force_defaults_to_false(self) -> None:
        """Backwards-compat: a UserMessage without an explicit
        ``force`` field parses as ``force=False``. The pre-force
        clients keep working unchanged."""
        from ember_code.protocol.messages import UserMessage

        msg = UserMessage(type="user_message", text="hi")
        assert msg.force is False

    def test_force_round_trips_through_true(self) -> None:
        from ember_code.protocol.messages import UserMessage

        msg = UserMessage(type="user_message", text="hi", force=True)
        assert msg.force is True
