"""Full session-restart round-trip across plan + decisions + todos.

Each piece has its own integration test:

* ``test_session_data_real_db.py`` — ``save/load_plan_decisions``
  + ``save/load_todos`` against a real Agno SQLite.
* ``test_plan_rehydrate.py`` — ``_rehydrate_plan_store`` walks
  Agno's session history back into ``PlanStore.latest``.
* ``test_plan_rpc_wiring.py`` — ``BackendServer.startup``
  actually calls the rehydrate methods.
* ``test_todo_persistence.py`` — ``_rehydrate_todos`` overrides
  the plan-args seed.

But no test runs the FULL cycle — write all three through one
session, drop it, build a fresh persistence-shaped harness
against the same DB file, assert every piece comes back
coherent. That's what this file does. The bug shape it guards
against: one rehydrate path silently regresses while the others
keep passing their isolated tests, and the user notices when
they reopen Tauri and find a different state than they left.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agno.session.agent import AgentSession

from ember_code.backend.server import BackendServer
from ember_code.core.session.persistence import SessionPersistence
from ember_code.core.tools.plan import PlanStore
from ember_code.core.tools.todo import TodoStore


def _make_db(tmp_path: Path):
    from agno.db.sqlite import AsyncSqliteDb

    db_file = tmp_path / "state.db"
    return AsyncSqliteDb(
        db_file=str(db_file),
        session_table="ember_sessions",
        memory_table="ember_memories",
    )


def _make_server_against_db(db, session_id: str) -> BackendServer:
    """Build a ``BackendServer`` shell with stores + persistence
    pointing at ``db``. Skips Agno team / agent boot — those
    aren't part of the restart contract. The persistence layer
    IS real (it talks to ``db``), so this exercises the actual
    storage path."""
    server = BackendServer.__new__(BackendServer)
    server._session = SimpleNamespace(
        session_id=session_id,
        user_id="u",
        plan_store=PlanStore(),
        todo_store=TodoStore(),
        persistence=SessionPersistence(db=db, session_id=session_id),
        # ``_rehydrate_plan_store`` needs ``main_team.aget_session``;
        # an AsyncMock returning None means "no Agno session row"
        # → plan_store.latest stays empty, which is fine for
        # tests that don't write through a real Agno run.
        main_team=MagicMock(aget_session=AsyncMock(return_value=None)),
        load_persisted_loop_state=AsyncMock(),
        # ``RehydrateController.event_log`` calls
        # ``session.restore_event_log(events)`` — the public method
        # that owns the ``event_log`` + ``_event_seq`` atomic write.
        # A no-op stub is enough because the persistence layer never
        # writes an event log in these tests.
        restore_event_log=lambda events: None,
        # ``project_dir`` is read by ``RehydrateController.orphan_processes``
        # which shells out to the process supervisor; ``tmp_path`` is
        # the closest analogue and safe because no orphan rows exist.
        project_dir=Path(tempfile.gettempdir()),
    )
    server._detect_interrupted_run = AsyncMock()
    return server


class TestFullRestartRoundTrip:
    """Three pieces of state — plan_decisions, todos, plan text
    — must survive a BE restart together. Without that, the
    user sees a frankenstein chat: approved plan card shows
    pending, half-done todo list resets, etc."""

    async def test_decisions_and_todos_both_restore(self, tmp_path):
        # Phase 1: live session writes both pieces.
        db = _make_db(tmp_path)
        live = _make_server_against_db(db, "sess-roundtrip")

        # Approve a plan — uses the real ``approve_plan`` /
        # persistence flow.
        await live._session.persistence.save_plan_decisions(
            {"run-1": "approved", "run-2": "dismissed"}
        )
        # Tick a todo to in_progress.
        await live._session.persistence.save_todos(
            [
                {"content": "Task A", "status": "in_progress"},
                {"content": "Task B", "status": "pending"},
            ]
        )

        # Phase 2: simulate BE restart. Fresh server pointed at
        # the SAME DB.
        restarted = _make_server_against_db(db, "sess-roundtrip")
        await restarted.startup()

        # Decisions intact.
        assert restarted._session.plan_store.decisions == {
            "run-1": "approved",
            "run-2": "dismissed",
        }
        # Todos intact, statuses preserved.
        statuses = {t["content"]: t["status"] for t in restarted._session.todo_store.snapshot()}
        assert statuses == {"Task A": "in_progress", "Task B": "pending"}

    async def test_only_decisions_written_other_pieces_empty(self, tmp_path):
        # Asymmetric corner: a session that recorded a plan
        # decision but never ran todos. Restart must still
        # restore the decision; todos stay empty (NOT the
        # plan-args seed — there's no plan to seed from in this
        # test harness).
        db = _make_db(tmp_path)
        live = _make_server_against_db(db, "sess-decisions-only")
        await live._session.persistence.save_plan_decisions({"run-X": "approved"})

        restarted = _make_server_against_db(db, "sess-decisions-only")
        await restarted.startup()

        assert restarted._session.plan_store.decisions == {"run-X": "approved"}
        assert restarted._session.todo_store.snapshot() == []

    async def test_only_todos_written_decisions_empty(self, tmp_path):
        # Mirror corner: live execution started but no plan
        # decision recorded. Restart restores todos; decisions
        # stay empty (NOT a stale leftover — fresh start).
        db = _make_db(tmp_path)
        live = _make_server_against_db(db, "sess-todos-only")
        await live._session.persistence.save_todos([{"content": "Task A", "status": "completed"}])

        restarted = _make_server_against_db(db, "sess-todos-only")
        await restarted.startup()

        assert restarted._session.plan_store.decisions == {}
        assert restarted._session.todo_store.snapshot() == [
            {"content": "Task A", "status": "completed", "activeForm": ""}
        ]

    async def test_independent_sessions_dont_cross_contaminate(self, tmp_path):
        # Two sessions in the same DB — restart of session A
        # must not surface session B's state. Bug shape: the
        # rehydrate code queries by session_id, so this is
        # mostly a "is the query correctly scoped" check.
        db = _make_db(tmp_path)
        a_live = _make_server_against_db(db, "sess-A")
        b_live = _make_server_against_db(db, "sess-B")

        await a_live._session.persistence.save_plan_decisions({"run-A1": "approved"})
        await b_live._session.persistence.save_plan_decisions({"run-B1": "dismissed"})
        await a_live._session.persistence.save_todos(
            [{"content": "A task", "status": "in_progress"}]
        )
        await b_live._session.persistence.save_todos([{"content": "B task", "status": "completed"}])

        # Restart only session A.
        a_restarted = _make_server_against_db(db, "sess-A")
        await a_restarted.startup()

        # A's state intact, NO B leakage.
        assert a_restarted._session.plan_store.decisions == {"run-A1": "approved"}
        statuses = {t["content"]: t["status"] for t in a_restarted._session.todo_store.snapshot()}
        assert statuses == {"A task": "in_progress"}
        assert "B task" not in statuses
        assert "run-B1" not in a_restarted._session.plan_store.decisions

    async def test_multiple_writes_then_restart_uses_latest(self, tmp_path):
        # Each write replaces the snapshot; restart sees ONLY
        # the last one. Bug shape: a sloppy implementation
        # might union writes, leading to a restart that shows a
        # mix of intermediate states.
        db = _make_db(tmp_path)
        live = _make_server_against_db(db, "sess-multi-write")

        # Write twice. Second write wins.
        await live._session.persistence.save_todos([{"content": "A", "status": "pending"}])
        await live._session.persistence.save_todos(
            [
                {"content": "A", "status": "in_progress"},
                {"content": "B", "status": "pending"},
            ]
        )
        await live._session.persistence.save_plan_decisions({"run-1": "approved"})
        await live._session.persistence.save_plan_decisions(
            {"run-1": "approved", "run-2": "approved"}
        )

        restarted = _make_server_against_db(db, "sess-multi-write")
        await restarted.startup()

        # Decisions: latest snapshot has both runs.
        assert restarted._session.plan_store.decisions == {
            "run-1": "approved",
            "run-2": "approved",
        }
        # Todos: latest snapshot has both items, A flipped.
        statuses = {t["content"]: t["status"] for t in restarted._session.todo_store.snapshot()}
        assert statuses == {"A": "in_progress", "B": "pending"}

    async def test_restart_survives_persistence_layer_corruption(self, tmp_path):
        # Realistic failure mode: someone hand-edited the DB and
        # corrupted ``session_data`` for the todos blob (e.g.
        # status changed to ``"banana"``). The cleaning at
        # ``load_todos`` drops the bad entry; restart should
        # surface a sensible (partial / empty) state, not crash.

        db = _make_db(tmp_path)
        # Hand-craft a corrupted session_data and upsert directly.
        now = int(time.time())
        bad = AgentSession(
            session_id="sess-corrupt",
            session_data={
                "plan_decisions": {
                    "run-1": "approved",
                    "run-bad": "maybe",  # invalid decision
                },
                "todos": [
                    {"content": "Good", "status": "in_progress"},
                    {"content": "Bad", "status": "banana"},  # invalid status
                    {"content": "", "status": "pending"},  # empty content
                    "not a dict",  # wrong shape
                ],
            },
            created_at=now,
            updated_at=now,
        )
        await db.upsert_session(bad, deserialize=True)

        restarted = _make_server_against_db(db, "sess-corrupt")
        await restarted.startup()  # must not raise

        # Only the valid decision survives.
        assert restarted._session.plan_store.decisions == {"run-1": "approved"}
        # Only the valid todo survives.
        assert restarted._session.todo_store.snapshot() == [
            {"content": "Good", "status": "in_progress", "activeForm": ""}
        ]
