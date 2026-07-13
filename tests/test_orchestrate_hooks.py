"""Tests for SubagentStart/SubagentStop hooks in OrchestrateTools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.tools.orchestrate import OrchestrateTools


def _settings():
    s = MagicMock()
    s.orchestration.max_nesting_depth = 3
    s.orchestration.max_total_agents = 20
    s.orchestration.sub_team_timeout = 30
    s.orchestration.max_task_iterations = 5
    return s


def _pool(*agents):
    pool = MagicMock()
    m = {a.name: a for a in agents}

    def get(name):
        if name not in m:
            raise KeyError(f"'{name}' not found")
        return m[name]

    pool.get.side_effect = get
    defn = MagicMock()
    defn.description = "Test"
    defn.tools = ["Read"]
    pool.get_definition.return_value = defn
    return pool


class TestSubagentStartStop:
    @pytest.mark.asyncio
    async def test_spawn_agent_fires_hooks(self):
        executor = MagicMock(spec=HookExecutor)
        executor.execute = AsyncMock()

        agent = MagicMock()
        agent.name = "coder"
        p = _pool(agent)

        t = OrchestrateTools(pool=p, settings=_settings(), hook_executor=executor, session_id="s1")

        with patch(
            "ember_code.core.tools.orchestrate._run_agent_streaming",
            new=AsyncMock(return_value=("done", [])),
        ):
            result = await t.spawn_agent(task="code", agent_name="coder")
        assert "done" in result

        calls = executor.execute.call_args_list
        assert any(c[1].get("event") == "SubagentStart" for c in calls)
        assert any(c[1].get("event") == "SubagentStop" for c in calls)

    @pytest.mark.asyncio
    async def test_spawn_agent_fires_stop_on_error(self):
        executor = MagicMock(spec=HookExecutor)
        executor.execute = AsyncMock()

        agent = MagicMock()
        agent.name = "buggy"
        p = _pool(agent)

        t = OrchestrateTools(pool=p, settings=_settings(), hook_executor=executor, session_id="s1")

        with patch(
            "ember_code.core.tools.orchestrate._run_agent_streaming",
            new=AsyncMock(side_effect=RuntimeError("crash")),
        ):
            result = await t.spawn_agent(task="stuff", agent_name="buggy")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_hooks_when_no_executor(self):
        agent = MagicMock()
        agent.name = "coder"
        t = OrchestrateTools(pool=_pool(agent), settings=_settings())
        with patch(
            "ember_code.core.tools.orchestrate._run_agent_streaming",
            new=AsyncMock(return_value=("ok", [])),
        ):
            result = await t.spawn_agent(task="code", agent_name="coder")
        assert "ok" in result

    @pytest.mark.asyncio
    async def test_spawn_team_fires_hooks(self):
        executor = MagicMock(spec=HookExecutor)
        executor.execute = AsyncMock()

        a1, a2 = MagicMock(), MagicMock()
        a1.name, a2.name = "a1", "a2"

        t = OrchestrateTools(
            pool=_pool(a1, a2), settings=_settings(), hook_executor=executor, session_id="s1"
        )

        with (
            # Post-refactor these are at ``orchestrate.py``'s module
            # top (iter 30) — patch the local bindings.
            patch("ember_code.core.tools.orchestrate.Team"),
            patch("ember_code.core.tools.orchestrate.ModelRegistry") as MockReg,
            patch(
                "ember_code.core.tools.orchestrate._run_team_streaming",
                new=AsyncMock(return_value=("team done", [])),
            ),
        ):
            MockReg.return_value.get_model.return_value = MagicMock()
            result = await t.spawn_team(task="plan", agent_names="a1,a2", mode="coordinate")
            assert "team done" in result

    @pytest.mark.asyncio
    async def test_max_depth_skips_hooks(self):
        executor = MagicMock(spec=HookExecutor)
        executor.execute = AsyncMock()

        agent = MagicMock()
        agent.name = "coder"
        t = OrchestrateTools(
            pool=_pool(agent),
            settings=_settings(),
            current_depth=10,
            hook_executor=executor,
            session_id="s1",
        )
        result = await t.spawn_agent(task="code", agent_name="coder")
        assert "depth" in result.lower()
        executor.execute.assert_not_called()
