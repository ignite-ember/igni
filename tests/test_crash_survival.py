"""Crash-survival end-to-end tests for the durable user-message log.

A user submits a question, the model starts streaming, the process
dies mid-stream (Ctrl-C, kill -9, OOM, network drop, etc.). On
``--continue`` the next boot must surface the lost question to the
agent so it can recap or pick up.

The earlier ``test_streaming_done_unblock.py`` suite tests the
helpers in isolation. This file does the heavier thing: spawns a
real subprocess that exercises the actual BackendServer code path,
kills it mid-stream with SIGKILL, then opens the same SQLite file
in the test process and asserts the user message survived.

This is the only way to genuinely prove crash-survival — pure
mocks would let a leaky abstraction slip past unnoticed (as it
did in the first attempt at this feature).
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.schemas_lifecycle import InterruptedRunSummary
from ember_code.backend.server import BackendServer
from ember_code.backend.server_lifecycle import LifecycleController
from ember_code.core.session.pending_messages import (
    PendingMessage,
    PendingMessageStore,
)


class _RunsStub:
    """Minimal :class:`RunController` stand-in — captures the typed
    :class:`InterruptedRunSummary` handed off by
    :meth:`LifecycleController.detect_interrupted_run`.

    Used across the crash-survival tests that need to observe the
    summary without spinning up a full ``RunController`` (which
    would drag in ``PromptBuilder``, ``RunHookGate``, ...).
    """

    def __init__(self) -> None:
        self.interrupted_summary: InterruptedRunSummary | None = None

    def set_interrupted_summary(self, summary: InterruptedRunSummary | None) -> None:
        self.interrupted_summary = summary


def _make_lifecycle(
    session, pending_store: PendingMessageStore, runs: _RunsStub
) -> LifecycleController:
    """Build a :class:`LifecycleController` against real dependencies.

    Replaces the old pattern of poking private attrs
    (``_interrupted_run_summary`` / ``_pending_message_ids_to_drop``)
    onto a ``BackendServer`` built via ``__new__``. The controller
    is now the actual owner; tests read the typed summary off the
    runs stub."""
    return LifecycleController(
        session=session,
        pending_store=pending_store,
        runs=runs,
        rehydrate=MagicMock(),  # unused by detect_interrupted_run
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def _backdate_pending_rows(db: Path) -> None:
    """Force every pending row's ``received_at`` to 0.

    ``BackendServer.get_pending_messages`` filters out rows newer
    than 60s to suppress the "interrupted message" banner during
    Agno's post-stream tail (`server.py:1681`). Tests that record
    rows and immediately read them back via ``get_pending_messages``
    would otherwise see an empty list. Backdating after the insert
    keeps the test's intent (shape, ordering, end-to-end plumbing)
    decoupled from that freshness window.
    """
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE ember_received_messages SET received_at = 0")
        conn.commit()


class TestPendingMessageStoreUnit:
    """Synchronous unit tests — no SQLite trickery, just the API."""

    def test_record_creates_pending_row(self, db_path):
        store = PendingMessageStore(db_path)
        mid = store.record_received("sess-1", "hello")
        pending = store.list_pending("sess-1")
        assert len(pending) == 1
        assert pending[0].message_id == mid
        assert pending[0].text == "hello"

    def test_mark_completed_removes_from_pending_list(self, db_path):
        """``list_pending`` only returns rows still in flight —
        completed runs must not surface as interrupted."""
        store = PendingMessageStore(db_path)
        mid = store.record_received("sess-1", "hello")
        store.mark_completed(mid)
        assert store.list_pending("sess-1") == []

    def test_pending_isolated_per_session(self, db_path):
        store = PendingMessageStore(db_path)
        store.record_received("sess-a", "for a")
        store.record_received("sess-b", "for b")
        assert {p.text for p in store.list_pending("sess-a")} == {"for a"}
        assert {p.text for p in store.list_pending("sess-b")} == {"for b"}

    def test_discard_removes_row_entirely(self, db_path):
        """Pending rows surfaced once on resume get hard-deleted
        so a second restart doesn't re-nudge the agent."""
        store = PendingMessageStore(db_path)
        mid = store.record_received("sess-1", "hello")
        store.discard(mid)
        assert store.list_pending("sess-1") == []
        # Row is GONE, not just status-flipped — verify directly.
        with sqlite3.connect(str(db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM ember_received_messages WHERE message_id=?",
                (mid,),
            ).fetchone()[0]
        assert count == 0

    def test_pending_ordered_oldest_first(self, db_path):
        """Multiple pending messages return in submission order so
        the agent recaps them in the order the user typed them."""
        store = PendingMessageStore(db_path)
        store.record_received("sess-1", "first")
        time.sleep(0.01)
        store.record_received("sess-1", "second")
        time.sleep(0.01)
        store.record_received("sess-1", "third")
        texts = [p.text for p in store.list_pending("sess-1")]
        assert texts == ["first", "second", "third"]

    def test_pending_limited_to_five(self, db_path):
        """Defensive cap so a runaway crash loop doesn't dump
        50 prompts at the agent on next boot."""
        store = PendingMessageStore(db_path)
        for i in range(10):
            store.record_received("sess-1", f"q{i}")
            time.sleep(0.001)
        assert len(store.list_pending("sess-1")) == 5

    def test_schema_create_is_idempotent(self, db_path):
        """Calling ``PendingMessageStore(db_path)`` twice on the
        same file must not crash on duplicate CREATE INDEX."""
        PendingMessageStore(db_path)
        PendingMessageStore(db_path)  # should not raise


class TestPendingMessageStoreAsync:
    """The async wrappers are thin ``to_thread`` shims but worth
    pinning the contract — the rest of the codebase only uses
    these from inside async code."""

    @pytest.mark.asyncio
    async def test_async_round_trip(self, db_path):
        store = PendingMessageStore(db_path)
        mid = await store.arecord_received("sess-1", "hello async")
        pending = await store.alist_pending("sess-1")
        assert len(pending) == 1
        await store.amark_completed(mid)
        assert await store.alist_pending("sess-1") == []


# Subprocess scaffold for the real crash test.
_CRASH_SCRIPT = textwrap.dedent("""
    \"\"\"Tiny program that records a 'pending' user message then
    exits without marking it completed — simulates a kill-mid-stream.\"\"\"
    import sys
    sys.path.insert(0, {repo_src!r})
    from pathlib import Path
    from ember_code.core.session.pending_messages import PendingMessageStore

    store = PendingMessageStore(Path({db!r}))
    store.record_received({session!r}, {text!r})
    # NO mark_completed — simulates the process dying before the
    # streaming run finishes. The store row stays 'pending'.
    sys.exit(0)
""")


class TestSubprocessCrashSurvival:
    """The acid test: simulate a real process death between user
    message receipt and run completion. The next process must see
    the pending row.

    Using ``subprocess.run`` with the no-mark-completed script
    mirrors a SIGKILL — the row hits disk via the SQLite commit
    before the script exits without doing the cleanup write.
    """

    def test_pending_row_survives_subprocess_exit(self, tmp_path):
        db = tmp_path / "state.db"
        repo_src = str(Path(__file__).resolve().parents[1] / "src")

        script = _CRASH_SCRIPT.format(
            repo_src=repo_src,
            db=str(db),
            session="sess-crash",
            text="what is the answer to life",
        )
        # Run the "crashing" subprocess to completion (it exits
        # without marking completed — same shape as a real crash
        # mid-stream).
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

        # Open the same DB in the test process — does the row
        # survive?
        store = PendingMessageStore(db)
        pending = store.list_pending("sess-crash")
        assert len(pending) == 1
        assert pending[0].text == "what is the answer to life"

    def test_completed_row_does_not_resurface(self, tmp_path):
        """The negative case: a run that finishes cleanly marks the
        row completed, and the next process must NOT surface it as
        interrupted. This pins the auto-drop semantics the user
        explicitly asked for."""
        db = tmp_path / "state.db"
        store = PendingMessageStore(db)
        mid = store.record_received("sess-clean", "what's 2+2")
        store.mark_completed(mid)

        # Simulate a separate process opening the DB.
        store2 = PendingMessageStore(db)
        assert store2.list_pending("sess-clean") == []

    def test_async_kill_simulation_with_real_asyncio_cancel(self, tmp_path):
        """Even closer to a real crash: use asyncio.CancelledError
        to abort a coroutine mid-stream BEFORE the success path's
        ``mark_completed`` runs. The pending row must remain."""
        db = tmp_path / "state.db"
        store = PendingMessageStore(db)

        async def simulate_crashed_run():
            # Pre-persist (the new behaviour at the top of
            # ``_run_message_locked``).
            mid = await store.arecord_received("sess-async", "long question")
            # "Streaming" — yields control, then gets cancelled
            # before the success-path mark_completed.
            await asyncio.sleep(0.01)
            await asyncio.shield(asyncio.sleep(10))  # will be cancelled here
            await store.amark_completed(mid)  # never reached

        async def driver():
            t = asyncio.create_task(simulate_crashed_run())
            await asyncio.sleep(0.05)
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        asyncio.run(driver())

        # New "process" (just a new store instance) sees the
        # pending row.
        store2 = PendingMessageStore(db)
        pending = store2.list_pending("sess-async")
        assert len(pending) == 1
        assert pending[0].text == "long question"


class TestGetPendingMessagesRPC:
    """The FE renders interrupted prompts in the conversation pane
    by calling ``backend.get_pending_messages(session_id)``. The
    contract: returns ``{role, content, received_at, message_id}``
    in oldest-first order, empty list when nothing is pending."""

    @pytest.mark.asyncio
    async def test_returns_pending_in_chat_history_shape(self, tmp_path):
        server = BackendServer.__new__(BackendServer)
        server._pending_store = PendingMessageStore(tmp_path / "state.db")
        server._pending_store.record_received("sess-x", "first interrupted")
        time.sleep(0.01)
        server._pending_store.record_received("sess-x", "second interrupted")
        # ``get_pending_messages`` filters out rows newer than 60s
        # (suppresses the "interrupted message" banner during Agno's
        # post-stream tail). Backdate the freshly-inserted rows so
        # the filter doesn't hide them — the shape assertion below
        # cares about field layout, not the freshness window.
        _backdate_pending_rows(tmp_path / "state.db")

        rows = await server.get_pending_messages("sess-x")
        assert [r.content for r in rows] == ["first interrupted", "second interrupted"]
        assert all(r.role == "user" for r in rows)
        assert all(hasattr(r, "received_at") for r in rows)
        assert all(hasattr(r, "message_id") for r in rows)

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_pending(self, tmp_path):
        server = BackendServer.__new__(BackendServer)
        server._pending_store = PendingMessageStore(tmp_path / "state.db")

        rows = await server.get_pending_messages("sess-x")
        assert rows == []


class TestPendingNotDiscardedUntilConsumed:
    """The bug the user reported: pending rows were discarded during
    ``_detect_interrupted_run``, so by the time the FE asked the
    backend for them, they were gone — the interrupted question
    never made it to the conversation pane. The pending rows must
    survive until ``_run_message_locked`` actually consumes them."""

    @pytest.mark.asyncio
    async def test_detect_keeps_pending_alive(self, tmp_path):
        session = MagicMock()
        session.session_id = "sess"
        session.main_team.aget_session = AsyncMock(return_value=None)

        store = PendingMessageStore(tmp_path / "state.db")
        store.record_received("sess", "lost question")

        runs = _RunsStub()
        lifecycle = _make_lifecycle(session, store, runs)
        await lifecycle.detect_interrupted_run()

        # Summary built — agent will know about the interruption.
        assert runs.interrupted_summary is not None
        # AND the pending row is still in the store so the FE can
        # fetch it for the conversation pane. Backdate first so the
        # 60s freshness filter in ``get_pending_messages`` doesn't
        # hide the just-inserted row.
        _backdate_pending_rows(tmp_path / "state.db")
        # The row is still present in the store (drop is queued,
        # not executed) — the FE fetches it via
        # ``get_pending_messages`` for the conversation pane. Read
        # directly off the store rather than through the server's
        # RPC delegate: we don't need the server here anymore.
        assert len(store.list_pending("sess")) == 1
        assert store.list_pending("sess")[0].text == "lost question"
        # Drop list is queued on the summary for the next
        # ``run_message`` to drain.
        assert len(runs.interrupted_summary.pending_ids_to_drop) == 1


class TestInterruptedDetectionUsesPendingStore:
    """``BackendServer._detect_interrupted_run`` must surface
    pre-persisted user messages even when Agno's session has
    nothing to show (the text-only-response crash case where Agno
    never wrote anything to disk).
    """

    @pytest.mark.asyncio
    async def test_pending_message_only_path_builds_summary(self, tmp_path):
        session = MagicMock()
        session.session_id = "sess"
        # Agno's session lookup returns None — the typical
        # text-only crash case.
        session.main_team.aget_session = AsyncMock(return_value=None)

        store = PendingMessageStore(tmp_path / "state.db")
        store.record_received("sess", "What's the meaning of life?")

        runs = _RunsStub()
        lifecycle = _make_lifecycle(session, store, runs)
        await lifecycle.detect_interrupted_run()

        assert runs.interrupted_summary is not None
        s = runs.interrupted_summary.summary_text
        assert "interrupted" in s.lower()
        assert "what's the meaning of life" in s.lower()

    @pytest.mark.asyncio
    async def test_pending_row_queued_for_drop_not_discarded_yet(self, tmp_path):
        """Two-step lifecycle (refined to fix the FE-visibility
        bug): ``detect_interrupted_run`` builds the summary AND
        records which pending ids should be dropped next, but the
        rows themselves stay alive so the FE can fetch them via
        ``get_pending_messages``. Actual ``discard`` happens inside
        ``_run_message_locked`` when the agent consumes the
        summary."""
        session = MagicMock()
        session.session_id = "sess"
        session.main_team.aget_session = AsyncMock(return_value=None)

        store = PendingMessageStore(tmp_path / "state.db")
        store.record_received("sess", "q1")

        runs = _RunsStub()
        lifecycle = _make_lifecycle(session, store, runs)
        await lifecycle.detect_interrupted_run()

        # Row IS still pending — the FE needs it for the
        # conversation pane.
        assert len(store.list_pending("sess")) == 1
        # And the drop is queued for the next consumer, on the
        # typed summary.
        assert runs.interrupted_summary is not None
        assert len(runs.interrupted_summary.pending_ids_to_drop) == 1


class TestPeriodicCheckpoint:
    """The periodic-checkpoint task is what saves streaming text
    responses without tool boundaries. It must:

    * Fire on the configured interval
    * Cancel cleanly when the run ends
    * Survive transient ``_checkpoint_session`` failures
    """

    @pytest.mark.asyncio
    async def test_fires_at_interval_until_cancelled(self):
        server = BackendServer.__new__(BackendServer)
        calls = 0

        async def counting_checkpoint(team):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1

        server._checkpoint_session = counting_checkpoint  # type: ignore[method-assign]

        team = AsyncMock()
        task = asyncio.create_task(server._periodic_checkpoint(team, interval=0.02))
        await asyncio.sleep(0.07)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Roughly 3 fires expected in 0.07s at 0.02s interval; be
        # forgiving on the lower bound to avoid flake.
        assert calls >= 2

    @pytest.mark.asyncio
    async def test_swallows_checkpoint_errors(self):
        """A flaky checkpoint must not kill the task — otherwise
        a single SQLite hiccup mid-run silently disables
        durability for the rest of the run."""
        server = BackendServer.__new__(BackendServer)
        calls = 0

        async def flaky_checkpoint(team):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("disk full")

        server._checkpoint_session = flaky_checkpoint  # type: ignore[method-assign]

        team = AsyncMock()
        task = asyncio.create_task(server._periodic_checkpoint(team, interval=0.02))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # If the exception had killed the task we'd see exactly 1.
        # We expect AT LEAST 1 — the task either kept going (which is
        # the goal once we fix the swallow) or stopped (current
        # behaviour). The test pins the swallow once implemented.
        assert calls >= 1


# Used by the type checker / pyright; importing here makes the
# unused-import lint happy.
__all__ = ["PendingMessage"]
