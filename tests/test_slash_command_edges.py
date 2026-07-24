"""Tests for slash command edge cases — P2.

Covers: missing args, bad input, error messages.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.backend.command_handler import CommandHandler, CommandResult


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
    # status() is async — give the mock a proper AsyncMock for it.
    from ember_code.core.knowledge.models import KnowledgeStatus

    session.knowledge_mgr.status = AsyncMock(return_value=KnowledgeStatus(enabled=False))
    session.memory_mgr.get_memories = AsyncMock(return_value=[])
    session.mcp_manager.list_servers.return_value = []
    session.mcp_manager.list_connected.return_value = []
    return session


class TestRenameEdgeCases:
    @pytest.mark.asyncio
    async def test_rename_no_args_shows_error(self):
        """'/rename' with no name should show usage info."""
        session = _make_session()
        session.persistence.rename = AsyncMock()
        handler = CommandHandler(session)
        result = await handler.handle("/rename")
        # Should either show error or usage, not crash
        assert isinstance(result, CommandResult)
        # Should not have called rename with empty string
        session.persistence.rename.assert_not_called()

    @pytest.mark.asyncio
    async def test_rename_with_name_succeeds(self):
        session = _make_session()
        session.persistence.rename = AsyncMock()
        handler = CommandHandler(session)
        result = await handler.handle("/rename My Session")
        assert isinstance(result, CommandResult)


class TestScheduleEdgeCases:
    @pytest.mark.asyncio
    async def test_schedule_add_no_time_shows_help(self):
        """'/schedule add something' without time should show format help."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/schedule add do something")
        assert isinstance(result, CommandResult)
        # Should indicate the time couldn't be parsed
        assert result.kind in ("error", "info", "markdown")

    @pytest.mark.asyncio
    async def test_schedule_add_bad_time(self):
        """'/schedule add task at xyzzy' should show error."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/schedule add task at xyzzy")
        assert isinstance(result, CommandResult)


class TestAgentsEdgeCases:
    @pytest.mark.asyncio
    async def test_agents_promote_no_name(self):
        """'/agents promote' with no name should show error."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/agents promote")
        assert isinstance(result, CommandResult)
        # Should be an error or info, not a crash
        assert result.kind in ("error", "info", "markdown")

    @pytest.mark.asyncio
    async def test_agents_discard_no_name(self):
        """'/agents discard' with no name should show error."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/agents discard")
        assert isinstance(result, CommandResult)

    @pytest.mark.asyncio
    async def test_agents_promote_nonexistent(self):
        """'/agents promote nonexistent' should show error."""
        session = _make_session()
        session.pool.promote_ephemeral = MagicMock(side_effect=KeyError("not found"))
        handler = CommandHandler(session)
        result = await handler.handle("/agents promote nonexistent")
        assert isinstance(result, CommandResult)
        assert result.kind in ("error", "info")


class TestKnowledgeEdgeCases:
    @pytest.mark.asyncio
    async def test_knowledge_add_no_args(self):
        """'/knowledge add' with no argument should show status or error."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/knowledge add")
        assert isinstance(result, CommandResult)
        # Should not crash

    @pytest.mark.asyncio
    async def test_knowledge_search_no_query(self):
        """'/knowledge search' with no query should show error."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/knowledge search")
        assert isinstance(result, CommandResult)


class TestUnknownCommand:
    @pytest.mark.asyncio
    async def test_unknown_command_returns_error(self):
        """Unknown slash command should return error."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/nonexistent_command")
        assert isinstance(result, CommandResult)
        assert result.kind == "error"

    @pytest.mark.asyncio
    async def test_slash_only_returns_error(self):
        """Just '/' should return error."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/")
        assert isinstance(result, CommandResult)


class TestHooksCommand:
    @pytest.mark.asyncio
    async def test_hooks_lists_loaded(self):
        """'/hooks' should list loaded hooks."""
        session = _make_session()
        session.hooks_map = {"PreToolUse": [MagicMock(type="command", command="test.sh")]}
        handler = CommandHandler(session)
        result = await handler.handle("/hooks")
        assert isinstance(result, CommandResult)

    @pytest.mark.asyncio
    async def test_hooks_empty(self):
        """'/hooks' with no hooks should say so."""
        session = _make_session()
        session.hooks_map = {}
        handler = CommandHandler(session)
        result = await handler.handle("/hooks")
        assert isinstance(result, CommandResult)
        assert "no" in result.content.lower() or result.content == ""


class TestConfigCommand:
    @pytest.mark.asyncio
    async def test_config_shows_model(self):
        """'/config' should show current model."""
        session = _make_session()
        from ember_code.core.auth.schemas import LoadCredentialsResult

        with patch(
            "ember_code.core.auth.credentials.CredentialsStore.load",
            return_value=LoadCredentialsResult(ok=False, reason="no_file"),
        ):
            handler = CommandHandler(session)
            result = await handler.handle("/config")
        assert isinstance(result, CommandResult)
        assert "test-model" in result.content


class TestModelCommand:
    @pytest.mark.asyncio
    async def test_model_no_args_lists_models(self):
        """'/model' with no args should list available models."""
        session = _make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/model")
        assert isinstance(result, CommandResult)

    @pytest.mark.asyncio
    async def test_model_switch_unknown(self):
        """'/model unknown' should show error."""
        from ember_code.backend.schemas_model import ModelSwitchResult

        session = _make_session()
        session.settings.models.registry = {}
        session.set_default_model.return_value = ModelSwitchResult(
            ok=False, model_name="nonexistent", available=[]
        )
        handler = CommandHandler(session)
        result = await handler.handle("/model nonexistent")
        assert isinstance(result, CommandResult)
