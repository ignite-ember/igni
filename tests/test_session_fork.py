"""Tests for session forking — both the persistence-layer
``SessionPersistence.fork`` and the ``/fork`` slash command
that re-binds the session components.

History note: this path has been bug-prone in the past
(8-char vs 32-char session IDs, missed re-binds on
main_team after fork) — the tests below lock in the
contract that surfaced during the row 41 audit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.session.persistence import SessionPersistence
from ember_code.protocol.messages import CommandAction


def _make_source_session(
    session_data: dict | None = None,
    runs: list | None = None,
    team_data: dict | None = None,
):
    """Build a stand-in Agno session object that ``db.get_session``
    can return. Mirrors the attributes ``fork`` mutates +
    upsertss."""
    source = MagicMock()
    source.session_id = "source01"
    source.session_data = session_data or {"session_name": "Original"}
    source.team_data = team_data or {"team_key": "team_val"}
    source.metadata = {"meta_key": "meta_val"}
    source.runs = runs if runs is not None else ["run1", "run2"]
    source.summary = MagicMock(summary="A summary")
    source.created_at = 100
    source.updated_at = 200
    return source


# ── SessionPersistence.fork ──────────────────────────────────


class TestPersistenceFork:
    @pytest.mark.asyncio
    async def test_raises_when_no_db(self):
        p = SessionPersistence(db=None, session_id="s1")
        with pytest.raises(RuntimeError, match="unavailable"):
            await p.fork()

    @pytest.mark.asyncio
    async def test_raises_when_source_missing(self):
        db = MagicMock()
        db.get_session = AsyncMock(return_value=None)
        p = SessionPersistence(db=db, session_id="ghost")
        with pytest.raises(RuntimeError, match="source session not found"):
            await p.fork()

    @pytest.mark.asyncio
    async def test_returns_short_8char_id(self):
        """The history note: previously this returned ``uuid4().hex``
        (32 chars), which read as a wall of hex in the UI. Lock in
        the 8-char prefix scheme so a future refactor can't drift
        back."""
        source = _make_source_session()
        db = MagicMock()
        db.get_session = AsyncMock(return_value=source)
        db.upsert_session = AsyncMock()
        p = SessionPersistence(db=db, session_id="source01")
        new_id = await p.fork()
        assert isinstance(new_id, str)
        assert len(new_id) == 8

    @pytest.mark.asyncio
    async def test_upserts_with_new_id(self):
        """The source row stays put; a NEW row is created under
        the fresh id — confirmed by checking ``upsert_session``
        was called with ``session_id`` set to the returned id."""
        source = _make_source_session()
        db = MagicMock()
        db.get_session = AsyncMock(return_value=source)
        db.upsert_session = AsyncMock()
        p = SessionPersistence(db=db, session_id="source01")
        new_id = await p.fork()

        db.upsert_session.assert_awaited_once()
        upserted = db.upsert_session.call_args[0][0]
        assert upserted.session_id == new_id
        # And the source row's id was mutated to the new value —
        # that's how the upsert key gets set. Mutating in place is
        # documented in ``fork``'s implementation.
        assert source.session_id == new_id

    @pytest.mark.asyncio
    async def test_refreshes_timestamps(self):
        source = _make_source_session()
        # Source was created in the past.
        source.created_at = 100
        source.updated_at = 200
        db = MagicMock()
        db.get_session = AsyncMock(return_value=source)
        db.upsert_session = AsyncMock()
        p = SessionPersistence(db=db, session_id="source01")
        await p.fork()
        # The fork should have updated both timestamps to "now"
        # (which is > the source's old timestamps).
        assert source.created_at > 200
        assert source.updated_at > 200
        assert source.created_at == source.updated_at

    @pytest.mark.asyncio
    async def test_preserves_runs_and_data(self):
        """Fork should COPY the conversation history — that's
        the whole point. Without this the fork would be a fresh
        session, not a fork."""
        source = _make_source_session(
            session_data={"session_name": "Original", "extra": "kept"},
            runs=["r1", "r2", "r3"],
            team_data={"team_state": {"key": "val"}},
        )
        db = MagicMock()
        db.get_session = AsyncMock(return_value=source)
        db.upsert_session = AsyncMock()
        p = SessionPersistence(db=db, session_id="source01")
        await p.fork()

        upserted = db.upsert_session.call_args[0][0]
        assert upserted.runs == ["r1", "r2", "r3"]
        assert upserted.team_data == {"team_state": {"key": "val"}}
        # ``extra`` field in session_data is preserved through fork.
        assert upserted.session_data["extra"] == "kept"

    @pytest.mark.asyncio
    async def test_name_arg_overrides_session_name(self):
        source = _make_source_session(
            session_data={"session_name": "Original", "extra": "kept"},
        )
        db = MagicMock()
        db.get_session = AsyncMock(return_value=source)
        db.upsert_session = AsyncMock()
        p = SessionPersistence(db=db, session_id="source01")
        await p.fork(name="My Fork")

        # ``session_name`` overridden; sibling fields preserved.
        assert source.session_data["session_name"] == "My Fork"
        assert source.session_data["extra"] == "kept"

    @pytest.mark.asyncio
    async def test_no_name_arg_keeps_source_name(self):
        """Without an explicit name the fork inherits the
        source's name — same as a freshly-created session that
        auto-names after the first run."""
        source = _make_source_session(
            session_data={"session_name": "Original"},
        )
        db = MagicMock()
        db.get_session = AsyncMock(return_value=source)
        db.upsert_session = AsyncMock()
        p = SessionPersistence(db=db, session_id="source01")
        await p.fork()

        assert source.session_data["session_name"] == "Original"


# ── CommandHandler /fork ─────────────────────────────────────


class TestForkSlashCommand:
    def _session(self, fork_result="newid01"):
        session = MagicMock()
        session.session_id = "source01"
        session.session_named = True
        session.main_team = MagicMock()
        session.main_team.session_id = "source01"
        session.persistence = MagicMock()
        session.persistence.session_id = "source01"
        session.persistence.fork = AsyncMock(return_value=fork_result)
        return session

    @pytest.mark.asyncio
    async def test_fork_delegates_rebind_to_session_rotate_id(self):
        """SAFETY: /fork MUST rebind the session id via
        :meth:`Session.rotate_id`, which owns the three-attribute
        invariant (session_id + main_team.session_id +
        persistence.session_id) atomically. Missing that call
        leaves the next turn reading/writing the SOURCE session.

        Coordinator-level test: verify /fork calls rotate_id with
        the fork's new id — the three-attribute invariant lives
        on Session.rotate_id and is covered by Session's own unit
        tests."""
        session = self._session(fork_result="forkedid")
        handler = CommandHandler(session)
        result = await handler.handle("/fork")
        # CommandResult signals the fork to the FE.
        assert result.action == CommandAction.FORK
        # Coordinator delegated to the invariant owner.
        session.rotate_id.assert_called_once_with("forkedid")

    @pytest.mark.asyncio
    async def test_fork_with_name_flags_session_named_true(self):
        """``session_named`` is the flag that says "user picked a
        name, don't auto-rename after the first run." When /fork
        carries a name, the new session inherits the flag — the
        auto-name logic must NOT overwrite the user's choice."""
        session = self._session()
        handler = CommandHandler(session)
        await handler.handle("/fork My Project Branch")
        # fork() called with the name.
        session.persistence.fork.assert_awaited_once_with(name="My Project Branch")
        assert session.session_named is True

    @pytest.mark.asyncio
    async def test_fork_without_name_flags_session_named_false(self):
        """No name → the fork looks like a freshly-minted session
        and the auto-name flow runs on first response."""
        session = self._session()
        handler = CommandHandler(session)
        await handler.handle("/fork")
        session.persistence.fork.assert_awaited_once_with(name=None)
        assert session.session_named is False

    @pytest.mark.asyncio
    async def test_fork_failure_returns_error(self):
        """Persistence-layer raise → error CommandResult; session
        state must NOT be mutated."""
        session = self._session()
        session.persistence.fork = AsyncMock(side_effect=RuntimeError("db gone"))
        handler = CommandHandler(session)
        result = await handler.handle("/fork")
        assert "Fork failed" in result.content
        assert "db gone" in result.content
        # State unchanged on failure.
        assert session.session_id == "source01"
        assert session.main_team.session_id == "source01"
        assert session.persistence.session_id == "source01"

    @pytest.mark.asyncio
    async def test_whitespace_only_name_treated_as_no_name(self):
        """``/fork   `` (spaces) should NOT be passed as the
        name — ``args.strip() or None`` collapses to None."""
        session = self._session()
        handler = CommandHandler(session)
        await handler.handle("/fork    ")
        session.persistence.fork.assert_awaited_once_with(name=None)
