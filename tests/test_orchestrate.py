"""Tests for tools/orchestrate.py — agent and team spawning."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.tools.orchestrate import OrchestrateTools


async def _mock_stream(content="agent response"):
    from agno.run import agent as ae

    # Real Agno streams open with RunStartedEvent — the orchestrate
    # code captures run_id/session_id from it so it can later look up
    # the canonical RunOutput via ``aget_run_output``. Without this the
    # final-answer fetch is skipped.
    started = MagicMock(spec=ae.RunStartedEvent)
    started.run_id = "fake-run-1"
    started.session_id = "fake-session"
    started.__class__ = ae.RunStartedEvent
    yield started

    event = MagicMock(spec=ae.RunContentEvent)
    event.content = content
    event.__class__ = ae.RunContentEvent
    yield event


def _mock_pool(content: str = "agent response"):
    pool = MagicMock()
    agent = MagicMock()
    agent.arun = MagicMock(return_value=_mock_stream(content))
    # Mirror Agno: after the run completes, the final answer is
    # available via ``aget_last_run_output`` / ``aget_run_output``
    # backed by the session DB.
    run_output = MagicMock()
    run_output.content = content
    agent.aget_run_output = AsyncMock(return_value=run_output)
    agent.aget_last_run_output = AsyncMock(return_value=run_output)
    defn = MagicMock()
    defn.description = "Test agent"
    defn.tools = ["Read", "Write"]
    pool.get.return_value = agent
    pool.get_definition.return_value = defn
    return pool


def _settings():
    s = MagicMock()
    s.orchestration.max_nesting_depth = 5
    s.orchestration.max_total_agents = 20
    s.orchestration.sub_team_timeout = 600
    s.orchestration.max_task_iterations = 10
    return s


class TestOrchestrateTools:
    def test_registers_functions(self):
        t = OrchestrateTools(pool=_mock_pool(), settings=_settings())
        names = {f.name for f in t.functions.values()} | {
            f.name for f in t.async_functions.values()
        }
        assert "spawn_agent" in names
        assert "spawn_team" in names

    @pytest.mark.asyncio
    async def test_spawn_agent_success(self):
        t = OrchestrateTools(pool=_mock_pool(), settings=_settings())
        result = await t.spawn_agent("Fix bug", "editor")
        assert "agent response" in result

    @pytest.mark.asyncio
    async def test_spawn_agent_depth_limit(self):
        t = OrchestrateTools(pool=_mock_pool(), settings=_settings(), current_depth=10)
        result = await t.spawn_agent("task", "editor")
        assert "depth" in result.lower()

    @pytest.mark.asyncio
    async def test_spawn_agent_shows_activity(self):
        result = await OrchestrateTools(pool=_mock_pool(), settings=_settings()).spawn_agent(
            "Fix", "editor"
        )
        assert "[Agent: editor]" in result
        assert "Activity:" in result

    @pytest.mark.asyncio
    async def test_spawn_team_single_delegates(self):
        result = await OrchestrateTools(pool=_mock_pool(), settings=_settings()).spawn_team(
            "task", "editor"
        )
        assert "[Agent: editor]" in result

    @pytest.mark.asyncio
    async def test_spawn_team_success(self):
        t = OrchestrateTools(pool=_mock_pool(), settings=_settings())
        with (
            # Post-refactor ``Team`` and ``ModelRegistry`` are imported
            # at ``orchestrate.py``'s module top (iter 30 Rule-2 sweep),
            # so we patch the local bindings. Patching the source module
            # wouldn't affect the names captured at import time.
            patch("ember_code.core.tools.orchestrate.Team"),
            patch("ember_code.core.tools.orchestrate.ModelRegistry") as MockReg,
            patch(
                "ember_code.core.tools.orchestrate._run_team_streaming",
                new=AsyncMock(return_value=("team result", [])),
            ),
        ):
            MockReg.return_value.get_model.return_value = MagicMock()
            result = await t.spawn_team("implement", "editor,explorer", mode="coordinate")
            assert "team result" in result
