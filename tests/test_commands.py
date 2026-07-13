"""Tests for session/commands.py — slash command dispatch (delegates to shared CommandHandler)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.session.commands import dispatch


def _make_session():
    """Create a mock Session with enough attributes for CommandHandler."""
    session = MagicMock()
    session._ensure_knowledge = AsyncMock()
    session.pool.list_agents.return_value = []
    session.pool.list_ephemeral.return_value = []
    session.skill_pool.list_skills.return_value = []
    session.skill_pool.match_user_command.return_value = None
    session.hooks_map = {}
    session.session_id = "abc12345"
    session.settings.models.default = "test-model"
    session.settings.models.registry = {"test-model": MagicMock()}
    session.settings.permissions.file_write = "ask"
    session.settings.permissions.shell_execute = "ask"
    session.settings.storage.backend = "sqlite"
    session.settings.memory.enable_agentic_memory = False
    session.settings.learning.enabled = False
    session.settings.reasoning.enabled = False
    session.settings.guardrails.pii_detection = False
    session.settings.guardrails.prompt_injection = False
    session.settings.guardrails.moderation = False
    session.settings.knowledge.enabled = False
    session.settings.orchestration.max_total_agents = 20
    session.settings.orchestration.max_nesting_depth = 3
    session.knowledge_mgr.share_enabled.return_value = False
    session.memory_mgr.get_memories = AsyncMock(return_value=[])
    session.mcp_manager.list_servers.return_value = []
    session.mcp_manager.list_connected.return_value = []
    session.code_index_sync.sync_now = AsyncMock()
    return session


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_known_command(self):
        session = _make_session()
        with patch("ember_code.core.session.commands.print_markdown"):
            result = await dispatch(session, "/agents")
        assert result is True

    @pytest.mark.asyncio
    async def test_dispatch_unknown_command(self):
        session = _make_session()
        result = await dispatch(session, "/nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_dispatch_help(self):
        session = _make_session()
        with patch("ember_code.core.session.commands.print_info"):
            result = await dispatch(session, "/help")
        assert result is True

    @pytest.mark.asyncio
    async def test_dispatch_help_with_topic(self):
        session = _make_session()
        with patch("ember_code.core.session.commands.print_markdown") as mock_print:
            result = await dispatch(session, "/help schedule")
        assert result is True
        printed = mock_print.call_args[0][0]
        assert "Schedule" in printed

    @pytest.mark.asyncio
    async def test_dispatch_config(self):
        session = _make_session()
        with (
            patch("ember_code.backend.command_handler.load_credentials", return_value=None),
            patch("ember_code.core.session.commands.print_markdown") as mock_print,
        ):
            result = await dispatch(session, "/config")
        assert result is True
        printed = mock_print.call_args[0][0]
        assert "test-model" in printed

    @pytest.mark.asyncio
    async def test_dispatch_clear(self):
        session = _make_session()
        with patch("ember_code.core.session.commands.print_info"):
            result = await dispatch(session, "/clear")
        assert result is True
        # Session id should have changed
        assert session.session_id != "abc12345"

    @pytest.mark.asyncio
    async def test_dispatch_mcp_action(self):
        session = _make_session()
        with patch("ember_code.core.session.commands.print_info") as mock_print:
            result = await dispatch(session, "/mcp")
        assert result is True
        printed = mock_print.call_args[0][0]
        assert "No MCP servers configured" in printed

    @pytest.mark.asyncio
    async def test_dispatch_model_action(self):
        session = _make_session()
        with patch("ember_code.core.session.commands.print_markdown") as mock_print:
            result = await dispatch(session, "/model")
        assert result is True
        printed = mock_print.call_args[0][0]
        assert "test-model" in printed

    @pytest.mark.asyncio
    async def test_dispatch_compact(self):
        session = _make_session()
        session.force_compact = AsyncMock(
            return_value=("Context compacted.", "Summary of conversation")
        )
        with patch("ember_code.core.session.commands.print_info"):
            result = await dispatch(session, "/compact")
        assert result is True
        session.force_compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_bug(self):
        session = _make_session()
        with (
            patch("webbrowser.open") as mock_open,
            patch("ember_code.core.session.commands.print_info"),
        ):
            result = await dispatch(session, "/bug")
        assert result is True
        mock_open.assert_called_once()
        assert "github" in mock_open.call_args[0][0]


class TestExtraCommands:
    @pytest.mark.asyncio
    async def test_sync_knowledge_not_enabled(self):
        session = _make_session()
        session.knowledge_mgr.share_enabled.return_value = False
        with patch("ember_code.core.session.commands.print_info") as mock_print:
            result = await dispatch(session, "/sync-knowledge")
        assert result is True
        assert "not enabled" in mock_print.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_sync_knowledge_runs_bidirectional(self):
        session = _make_session()
        session.knowledge_mgr.share_enabled.return_value = True
        r = MagicMock(direction="file_to_db", summary="Loaded 3 entries")
        session.knowledge_mgr.sync_bidirectional = AsyncMock(return_value=[r])
        with patch("ember_code.core.session.commands.print_info"):
            result = await dispatch(session, "/sync-knowledge")
        assert result is True
        session.knowledge_mgr.sync_bidirectional.assert_called_once()
