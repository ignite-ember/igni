"""Tests for session/memory_ops.py — user memory management."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.config.settings import Settings
from ember_code.core.session.memory_ops import SessionMemoryManager


class TestSessionMemoryManager:
    def test_create_manager_no_db(self):
        settings = Settings()
        mgr = SessionMemoryManager(db=None, settings=settings, user_id="test-user")
        result = mgr._create_manager()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_memories_no_db(self):
        settings = Settings()
        mgr = SessionMemoryManager(db=None, settings=settings, user_id="user")
        result = await mgr.get_memories()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_memories_returns_formatted(self):
        mem = MagicMock()
        mem.memory = "User prefers concise responses"
        mem.topics = ["preferences", "style"]

        mock_agent = MagicMock()
        mock_agent.aget_user_memories = AsyncMock(return_value=[mem])

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with patch.object(mgr, "_create_reader_agent", return_value=mock_agent):
            result = await mgr.get_memories()

        assert len(result) == 1
        assert result[0]["memory"] == "User prefers concise responses"
        assert "preferences" in result[0]["topics"]

    @pytest.mark.asyncio
    async def test_get_memories_handles_exception(self):
        mock_agent = MagicMock()
        mock_agent.aget_user_memories = AsyncMock(side_effect=RuntimeError("fail"))

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with patch.object(mgr, "_create_reader_agent", return_value=mock_agent):
            result = await mgr.get_memories()
        assert result == []

    @pytest.mark.asyncio
    async def test_optimize_no_manager(self):
        settings = Settings()
        mgr = SessionMemoryManager(db=None, settings=settings, user_id="user")
        result = await mgr.optimize()
        assert not result.success
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_optimize_not_enough_memories(self):
        mock_agent = MagicMock()
        mock_agent.aget_user_memories = AsyncMock(return_value=[MagicMock()])

        mock_manager = MagicMock()

        settings = Settings()
        mgr = SessionMemoryManager(db=MagicMock(), settings=settings, user_id="user")

        with (
            patch.object(mgr, "_create_manager", return_value=mock_manager),
            patch.object(mgr, "_create_reader_agent", return_value=mock_agent),
        ):
            result = await mgr.optimize()

        assert result.count_before == 1
        assert "Not enough" in result.message
