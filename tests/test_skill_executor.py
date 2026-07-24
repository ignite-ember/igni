"""Tests for skills/executor.py — skill execution engine."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.skills.executor import SkillExecutor, SkillResult


def _mock_skill(name="test-skill", context="default", agent="editor"):
    skill = MagicMock()
    skill.name = name
    skill.context = context
    skill.agent = agent
    skill.version = "0.1.0"
    skill.model = "inherit"
    skill.render.return_value = "Do the thing with $ARGUMENTS"
    return skill


def _mock_pool():
    pool = MagicMock()
    agent = MagicMock()
    agent.arun = AsyncMock(return_value=MagicMock(content="skill result"))
    pool.get.return_value = agent
    return pool


def _mock_settings():
    settings = MagicMock()
    settings.models.default = "MiniMax-M2.7"
    return settings


class TestSkillExecutor:
    @pytest.mark.asyncio
    async def test_execute_inline(self):
        pool = _mock_pool()
        executor = SkillExecutor(pool, _mock_settings())
        skill = _mock_skill(context="default")

        result = await executor.execute(skill, arguments="my args")
        assert isinstance(result, SkillResult)
        assert result.ok is True
        assert isinstance(result.text, str)
        skill.render.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_forked(self):
        pool = _mock_pool()
        executor = SkillExecutor(pool, _mock_settings())
        skill = _mock_skill(context="fork")

        result = await executor.execute(skill, arguments="test")
        assert isinstance(result, SkillResult)
        assert result.ok is True
        # Forked should call pool.get with the skill's agent name
        pool.get.assert_called_with("editor")

    @pytest.mark.asyncio
    async def test_passes_arguments_to_render(self):
        pool = _mock_pool()
        executor = SkillExecutor(pool, _mock_settings())
        skill = _mock_skill()

        await executor.execute(skill, arguments="deploy staging")
        call_args = skill.render.call_args
        assert "deploy staging" in str(call_args)
