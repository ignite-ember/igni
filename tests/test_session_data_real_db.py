"""Real-Agno-DB round-trip for ``SessionPersistence`` writes.

The unit tests for ``save_plan_decisions`` and ``save_todos`` use a
stub persistence layer — they verify the in-memory call shape but
DON'T exercise the actual Agno SQLite path. The live-BE Playwright
verification surfaced the gap: against a real BE that hadn't yet
written its session row, ``save_plan_decisions`` silently no-op'd
(get_session returned None → method returned). The fix creates a
minimal row when the session is missing.

These tests pin the real round-trip against an in-memory Agno
SQLite so the regression can't return:

* Persisted ``session_data["plan_decisions"]`` survives a fresh
  ``SessionPersistence`` instance reading it back.
* Persisted ``session_data["todos"]`` survives the same.
* The "session row doesn't exist yet" corner case (the live
  Playwright check exposed) creates the row instead of dropping
  the write.
* Co-existence: writing ``plan_decisions`` doesn't clobber a
  separately-written ``todos`` field, and vice versa.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.session.persistence import SessionPersistence


def _make_db(tmp_path: Path):
    """In-process Agno AsyncSqliteDb against a tempdir SQLite file.
    Same import path used by ``MemoryManager.create_db``."""
    from agno.db.sqlite import AsyncSqliteDb

    db_file = tmp_path / "state.db"
    return AsyncSqliteDb(
        db_file=str(db_file),
        session_table="ember_sessions",
        memory_table="ember_memories",
    )


class TestRealDbPlanDecisions:
    async def test_creates_row_when_session_missing(self, tmp_path):
        # The exact path the live Playwright check hit. Before
        # the fix this returned silently with nothing in the
        # DB; now it creates a minimal session row.
        db = _make_db(tmp_path)
        persistence = SessionPersistence(db=db, session_id="sess-new")
        await persistence.save_plan_decisions({"run-1": "approved"})

        loaded = await persistence.load_plan_decisions()
        assert loaded == {"run-1": "approved"}

    async def test_round_trip_updates_existing_row(self, tmp_path):
        # First write creates the row, second write updates it.
        # Without the "merge with existing" logic, the second
        # write would wipe out the first.
        db = _make_db(tmp_path)
        persistence = SessionPersistence(db=db, session_id="sess-upd")
        await persistence.save_plan_decisions({"run-1": "approved"})
        await persistence.save_plan_decisions({"run-1": "approved", "run-2": "dismissed"})
        loaded = await persistence.load_plan_decisions()
        assert loaded == {"run-1": "approved", "run-2": "dismissed"}

    async def test_fresh_persistence_instance_sees_prior_write(self, tmp_path):
        # The actual restart scenario: write through one
        # ``SessionPersistence`` instance, throw it away, build a
        # new one against the same DB, read back. Without
        # persistence this would return an empty dict — same as a
        # fresh boot would have shown the user pre-fix.
        db = _make_db(tmp_path)
        writer = SessionPersistence(db=db, session_id="sess-restart")
        await writer.save_plan_decisions({"run-x": "approved"})

        # Imagine BE restart: new persistence layer instance.
        reader = SessionPersistence(db=db, session_id="sess-restart")
        loaded = await reader.load_plan_decisions()
        assert loaded == {"run-x": "approved"}

    async def test_filters_invalid_entries_on_write(self, tmp_path):
        # The cleaning that already exists at the in-memory
        # layer must also hold against the real DB — a malformed
        # entry can't slip through to disk and corrupt future
        # reads.
        db = _make_db(tmp_path)
        persistence = SessionPersistence(db=db, session_id="sess-clean")
        await persistence.save_plan_decisions(
            {"good": "approved", "bad": "maybe", 42: "approved"}  # type: ignore[dict-item]
        )
        assert await persistence.load_plan_decisions() == {"good": "approved"}


class TestRealDbTodos:
    async def test_creates_row_when_session_missing(self, tmp_path):
        # Same corner case as plan_decisions — the live check
        # would have hit it for ``save_todos`` too if a user
        # somehow triggered ``todo_write`` on an empty session
        # (rare but the fallback shouldn't be sloppy).
        db = _make_db(tmp_path)
        persistence = SessionPersistence(db=db, session_id="sess-todos-new")
        await persistence.save_todos([{"content": "Task A", "status": "in_progress"}])

        loaded = await persistence.load_todos()
        assert loaded == [{"content": "Task A", "status": "in_progress", "activeForm": ""}]

    async def test_atomic_replace_via_real_db(self, tmp_path):
        # Each write replaces the snapshot. Without atomic
        # replace, an in_progress → completed flip would leave
        # both versions in the persisted blob and ``load_todos``
        # would return a confused list.
        db = _make_db(tmp_path)
        persistence = SessionPersistence(db=db, session_id="sess-todos-replace")
        await persistence.save_todos(
            [
                {"content": "A", "status": "in_progress"},
                {"content": "B", "status": "pending"},
            ]
        )
        await persistence.save_todos(
            [
                {"content": "A", "status": "completed"},
                {"content": "B", "status": "pending"},
            ]
        )
        loaded = await persistence.load_todos()
        statuses = {t["content"]: t["status"] for t in loaded}
        assert statuses == {"A": "completed", "B": "pending"}

    async def test_in_progress_survives_restart(self, tmp_path):
        # The headline regression — same scenario as the
        # mocked unit test but against real SQLite. Agent
        # flips A → in_progress, fresh persistence instance
        # reads it back unchanged.
        db = _make_db(tmp_path)
        writer = SessionPersistence(db=db, session_id="sess-todo-restart")
        await writer.save_todos(
            [
                {"content": "Refactor _mode_step", "status": "in_progress"},
                {"content": "Update tests", "status": "pending"},
            ]
        )

        reader = SessionPersistence(db=db, session_id="sess-todo-restart")
        loaded = await reader.load_todos()
        statuses = {t["content"]: t["status"] for t in loaded}
        assert statuses == {
            "Refactor _mode_step": "in_progress",
            "Update tests": "pending",
        }


class TestRealDbCoExistence:
    """plan_decisions and todos must coexist in session_data —
    writing one mustn't wipe the other. Bug shape this guards
    against: a sloppy ``session.session_data = {"plan_decisions": ...}``
    would overwrite a previously-written ``todos`` blob."""

    async def test_plan_then_todos_both_survive(self, tmp_path):
        db = _make_db(tmp_path)
        persistence = SessionPersistence(db=db, session_id="sess-coexist-a")
        await persistence.save_plan_decisions({"run-1": "approved"})
        await persistence.save_todos([{"content": "Task A", "status": "in_progress"}])
        assert await persistence.load_plan_decisions() == {"run-1": "approved"}
        assert await persistence.load_todos() == [
            {"content": "Task A", "status": "in_progress", "activeForm": ""}
        ]

    async def test_todos_then_plan_both_survive(self, tmp_path):
        db = _make_db(tmp_path)
        persistence = SessionPersistence(db=db, session_id="sess-coexist-b")
        await persistence.save_todos([{"content": "Task A", "status": "in_progress"}])
        await persistence.save_plan_decisions({"run-1": "approved"})
        assert await persistence.load_plan_decisions() == {"run-1": "approved"}
        assert await persistence.load_todos() == [
            {"content": "Task A", "status": "in_progress", "activeForm": ""}
        ]

    async def test_does_not_clobber_unrelated_session_data(self, tmp_path):
        # If something else (Agno itself, a future feature)
        # writes a different key into session_data, our
        # write must preserve it.
        import time

        from agno.db.base import SessionType
        from agno.session.agent import AgentSession

        db = _make_db(tmp_path)
        # Seed with a row that has an unrelated key already.
        # ``created_at``/``updated_at`` are NOT NULL in the
        # ember_sessions schema; the real run path fills them
        # via ``int(time.time())`` so we do the same.
        now = int(time.time())
        seeded = AgentSession(
            session_id="sess-merge",
            session_data={"session_name": "My Session", "custom_thing": 42},
            created_at=now,
            updated_at=now,
        )
        await db.upsert_session(seeded, deserialize=True)

        persistence = SessionPersistence(db=db, session_id="sess-merge")
        await persistence.save_plan_decisions({"run-1": "approved"})

        # Read back the FULL session_data — our write merged,
        # didn't replace.
        loaded = await db.get_session(
            session_id="sess-merge",
            session_type=SessionType.AGENT,
            deserialize=True,
        )
        assert loaded is not None
        sd = loaded.session_data or {}
        assert sd.get("session_name") == "My Session"
        assert sd.get("custom_thing") == 42
        assert sd.get("plan_decisions") == {"run-1": "approved"}
