"""Tests for session/persistence.py — session listing, naming, and resuming."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.session.persistence import SessionPersistence


class TestSessionPersistence:
    def test_stores_db_and_session_id(self):
        p = SessionPersistence(db=MagicMock(), session_id="abc")
        assert p.session_id == "abc"
        assert p.db is not None

    @pytest.mark.asyncio
    async def test_list_sessions_empty_when_no_db(self):
        p = SessionPersistence(db=None, session_id="s1")
        result = await p.list_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_sessions_returns_formatted(self):
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.runs = [1, 2]
        mock_session.summary = MagicMock(summary="Test session")
        mock_session.agent_data = {"name": "editor"}
        mock_session.session_data = {"session_name": "my session"}
        mock_session.created_at = 1000
        mock_session.updated_at = 2000

        db = MagicMock()
        db.get_sessions = AsyncMock(return_value=[mock_session])

        p = SessionPersistence(db=db, session_id="current")
        result = await p.list_sessions(limit=10)

        assert len(result) == 1
        assert result[0]["session_id"] == "s1"
        assert result[0]["name"] == "my session"
        assert result[0]["run_count"] == 2
        assert result[0]["summary"] == "Test session"

    @pytest.mark.asyncio
    async def test_list_sessions_handles_tuple_return(self):
        mock_session = MagicMock()
        mock_session.session_id = "s1"
        mock_session.runs = []
        mock_session.summary = None
        mock_session.agent_data = None
        mock_session.session_data = None
        mock_session.created_at = 0
        mock_session.updated_at = 0

        db = MagicMock()
        db.get_sessions = AsyncMock(return_value=([mock_session], 1))

        p = SessionPersistence(db=db, session_id="current")
        result = await p.list_sessions()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_list_sessions_handles_exception(self):
        db = MagicMock()
        db.get_sessions = AsyncMock(side_effect=RuntimeError("DB error"))

        p = SessionPersistence(db=db, session_id="s1")
        result = await p.list_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_sessions_default_limit_is_none(self):
        """Regression: earlier revisions hard-coded ``limit=20``,
        silently hiding older sessions from the FE. The default is
        now ``None`` (all) — the FE virtualises the list itself."""
        db = MagicMock()
        db.get_sessions = AsyncMock(return_value=[])
        p = SessionPersistence(db=db, session_id="s1")

        await p.list_sessions()

        db.get_sessions.assert_awaited_once()
        assert db.get_sessions.await_args.kwargs["limit"] is None

    @pytest.mark.asyncio
    async def test_list_sessions_explicit_limit_forwarded(self):
        """Callers that need capped output (CLI's boot-time preview
        passes ``limit=1``) still get honored."""
        db = MagicMock()
        db.get_sessions = AsyncMock(return_value=[])
        p = SessionPersistence(db=db, session_id="s1")

        await p.list_sessions(limit=1)

        assert db.get_sessions.await_args.kwargs["limit"] == 1

    @pytest.mark.asyncio
    async def test_auto_name_calls_aset_session_name(self):
        executor = MagicMock()
        executor.aset_session_name = AsyncMock()

        p = SessionPersistence(db=MagicMock(), session_id="s1")
        await p.auto_name(executor)

        executor.aset_session_name.assert_called_once_with(session_id="s1", autogenerate=True)

    @pytest.mark.asyncio
    async def test_auto_name_handles_missing_method(self):
        executor = MagicMock(spec=[])  # no aset_session_name
        p = SessionPersistence(db=MagicMock(), session_id="s1")
        await p.auto_name(executor)  # should not raise

    @pytest.mark.asyncio
    async def test_rename_no_db(self):
        p = SessionPersistence(db=None, session_id="s1")
        await p.rename("new name")  # should not raise

    @pytest.mark.asyncio
    async def test_rename_calls_db(self):
        db = MagicMock()
        db.rename_session = AsyncMock()

        p = SessionPersistence(db=db, session_id="s1")
        await p.rename("new name")

        db.rename_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_name_no_db(self):
        p = SessionPersistence(db=None, session_id="s1")
        result = await p.get_name()
        assert result == ""

    @pytest.mark.asyncio
    async def test_get_name_returns_session_name(self):
        mock_session = MagicMock()
        mock_session.session_data = {"session_name": "my session"}

        db = MagicMock()
        db.get_session = AsyncMock(return_value=mock_session)

        p = SessionPersistence(db=db, session_id="s1")
        result = await p.get_name()
        assert result == "my session"

    @pytest.mark.asyncio
    async def test_get_name_handles_exception(self):
        db = MagicMock()
        db.get_session = AsyncMock(side_effect=RuntimeError("fail"))

        p = SessionPersistence(db=db, session_id="s1")
        result = await p.get_name()
        assert result == ""
