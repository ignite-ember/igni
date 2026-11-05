"""Clearing, cutting or forking a session's history changes what is stored.

Agno 3 keeps each run in a row of its own and ``upsert_session`` writes
the session's row only, so compaction (clear), a rewind (cut) and a fork
(copy) that saved the session and trusted the runs to follow changed
nothing on disk. These run against Agno's real SQLite storage, the one a
desktop install uses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from agno.db.base import SessionType
from agno.db.sqlite import AsyncSqliteDb
from agno.models.message import Message
from agno.run.agent import RunInput, RunOutput
from agno.session.agent import AgentSession

from ember_code.core.db.agno_schema import AgnoSchema
from ember_code.core.session.persistence.forker import SessionForker
from ember_code.core.session.persistence.run_history import RunHistory

SESSION = "sess-history"


def _run(index: int, *, parent: str | None = None) -> RunOutput:
    return RunOutput(
        run_id=f"run-{index}",
        agent_id="ember",
        session_id=SESSION,
        parent_run_id=parent,
        input=RunInput(input_content=f"question {index}"),
        messages=[
            Message(role="user", content=f"question {index}"),
            Message(role="assistant", content=f"answer {index}"),
        ],
        content=f"answer {index}",
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncSqliteDb:
    store = AsyncSqliteDb(
        db_file=str(tmp_path / "state.db"),
        session_table="ember_sessions",
        memory_table="ember_memories",
    )
    session = AgentSession(
        session_id=SESSION,
        agent_id="ember",
        runs=[_run(0), _run(1), _run(2, parent="run-1")],
        session_data={},
        created_at=int(time.time()),
    )
    await RunHistory(store).save(session)
    return store


async def _runs(db: AsyncSqliteDb, session_id: str) -> list[str]:
    stored = await db.get_session(session_id=session_id, session_type=SessionType.AGENT)
    assert stored is not None
    return [run.run_id for run in stored.runs or []]


async def test_the_runs_are_stored_in_order(db: AsyncSqliteDb) -> None:
    assert await _runs(db, SESSION) == ["run-0", "run-1", "run-2"]


async def test_a_session_saved_without_its_runs_keeps_them_in_agno_3(db: AsyncSqliteDb) -> None:
    """The reason RunHistory exists: Agno's own save leaves the runs."""
    stored = await db.get_session(session_id=SESSION, session_type=SessionType.AGENT)
    stored.runs = []
    await db.upsert_session(stored)
    assert await _runs(db, SESSION) == ["run-0", "run-1", "run-2"]


async def test_clearing_the_history_clears_it(db: AsyncSqliteDb) -> None:
    stored = await db.get_session(session_id=SESSION, session_type=SessionType.AGENT)
    stored.runs = []
    await RunHistory(db).save(stored)
    assert await _runs(db, SESSION) == []


async def test_cutting_the_history_keeps_what_came_before(db: AsyncSqliteDb) -> None:
    stored = await db.get_session(session_id=SESSION, session_type=SessionType.AGENT)
    stored.runs = stored.runs[:1]
    await RunHistory(db).save(stored)
    assert await _runs(db, SESSION) == ["run-0"]


async def test_a_fork_has_the_history_and_the_source_keeps_its_own(db: AsyncSqliteDb) -> None:
    result = await SessionForker(db=db, session_id=SESSION).fork(name="copy")
    assert result.ok, result.error

    forked = await db.get_session(session_id=result.new_session_id, session_type=SessionType.AGENT)
    assert forked is not None
    assert [run.content for run in forked.runs] == ["answer 0", "answer 1", "answer 2"]
    assert {run.run_id for run in forked.runs}.isdisjoint({"run-0", "run-1", "run-2"})
    # The member's run still points at its parent, under the parent's new id.
    assert forked.runs[2].parent_run_id == forked.runs[1].run_id
    assert await _runs(db, SESSION) == ["run-0", "run-1", "run-2"]


# ── A database an earlier igni wrote ─────────────────────────────────

#: Written by igni on Agno 2.6.22: one session, its three runs in the row.
AGNO_2_6_DB = Path(__file__).parent / "fixtures" / "agno_2_6_state.db"


@pytest.fixture
def agno_2_6_db(tmp_path: Path) -> Path:
    copy = tmp_path / "state.db"
    copy.write_bytes(AGNO_2_6_DB.read_bytes())
    return copy


async def test_a_session_agno_2_6_wrote_reads_back_and_its_history_can_be_cut(
    agno_2_6_db: Path,
) -> None:
    """Migrated on open, its runs are in their own table and the old copy is gone.

    Without the migration the session still reads back — Agno merges the
    old in-row runs in — but a cut history returned from that copy.
    """
    AgnoSchema.ensure_current(
        agno_2_6_db, session_table="ember_sessions", memory_table="ember_memories"
    )
    db = AsyncSqliteDb(
        db_file=str(agno_2_6_db), session_table="ember_sessions", memory_table="ember_memories"
    )
    stored = await db.get_session(session_id="sess-26", session_type=SessionType.AGENT)
    assert stored is not None
    assert stored.session_data["session_name"] == "Made by Agno 2.6"
    assert [run.content for run in stored.runs] == ["answer 0", "answer 1", "answer 2"]
    assert await db.get_latest_schema_version("ember_sessions") == "3.0.0"

    stored.runs = stored.runs[:1]
    await RunHistory(db).save(stored)
    assert await _runs(db, "sess-26") == ["run-0"]
