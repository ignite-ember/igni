"""Tests for ``OrchestrateTools.spawn_agent(isolation="worktree")``
— Claude Code's per-spawn worktree isolation parity (row 30).

Layered:

* Unit tests for the standalone helpers (``_finalize_worktree``,
  ``_rebind_tool_base_dirs``, ``_create_isolated_worktree``) so
  failure modes have clear coverage.
* Integration tests that spin up a real git repo on ``tmp_path``
  and run ``spawn_agent`` with isolation enabled, asserting the
  worktree was created, the tool ``base_dir`` was rebased and
  restored, and the response carries the expected footer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.tools.orchestrate import OrchestrateTools, _finalize_worktree

# ── Shared fixtures (mirrored from test_orchestrate.py) ──────


def _mock_stream(content: str):
    from agno.run import agent as ae

    async def stream():
        started = MagicMock(spec=ae.RunStartedEvent)
        started.run_id = "r1"
        started.agent_id = "a1"
        started.__class__ = ae.RunStartedEvent
        yield started
        event = MagicMock(spec=ae.RunContentEvent)
        event.content = content
        event.__class__ = ae.RunContentEvent
        yield event

    return stream()


def _mock_pool(content: str = "agent response", agent_tools: list | None = None):
    pool = MagicMock()
    agent = MagicMock()
    agent.arun = MagicMock(return_value=_mock_stream(content))
    run_output = MagicMock()
    run_output.content = content
    agent.aget_run_output = AsyncMock(return_value=run_output)
    agent.aget_last_run_output = AsyncMock(return_value=run_output)
    agent.tools = agent_tools if agent_tools is not None else []
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


def _init_git_repo(path: Path) -> None:
    """Initialise an empty git repo with a committed file so
    ``git worktree add -b`` has a parent commit to fork from."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)


# ── _finalize_worktree (pure helper) ─────────────────────────


class TestFinalizeWorktree:
    def test_no_worktree_returns_empty(self):
        """The hot path: spawn without isolation. Call should be
        cheap and produce no footer."""
        assert _finalize_worktree(None, None, {}) == ""

    def test_restores_base_dirs_even_when_no_worktree(self):
        """A future caller might rebase tools without creating a
        worktree (custom isolation mode). ``_finalize_worktree``
        must still restore them."""
        tool = MagicMock()
        tool.base_dir = Path("/changed")
        _finalize_worktree(None, None, {tool: Path("/original")})
        assert tool.base_dir == Path("/original")

    def test_cleanup_failure_surfaces_as_footer(self):
        """If ``manager.cleanup()`` raises, the footer reports
        the failure rather than letting the exception escape."""
        info = MagicMock(worktree_path=Path("/tmp/wt"), branch_name="b1")
        manager = MagicMock()
        manager.cleanup.side_effect = RuntimeError("boom")
        footer = _finalize_worktree(manager, info, {})
        assert "cleanup failed" in footer
        assert "boom" in footer


# ── _rebind_tool_base_dirs ────────────────────────────────────


class _FakeTool:
    """Stand-in for a toolkit with a ``base_dir`` attribute. The
    real Agno Toolkits set this in __init__; bypassing the parent
    class avoids the registration machinery."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir


class TestRebindBaseDirs:
    def test_rebinds_each_tool_and_returns_originals(self):
        a = _FakeTool(Path("/orig/a"))
        b = _FakeTool(Path("/orig/b"))
        agent = MagicMock()
        agent.tools = [a, b]

        originals = OrchestrateTools._rebind_tool_base_dirs(agent, Path("/wt"))
        # All tools' base_dirs now point at the worktree.
        for tool in agent.tools:
            assert tool.base_dir == Path("/wt")
        # The map records the ORIGINAL paths so the caller can restore.
        assert set(originals.values()) == {Path("/orig/a"), Path("/orig/b")}
        # And the mapping uses the (shallow-copied) tools as keys.
        for tool in agent.tools:
            assert tool in originals

    def test_shallow_copies_tools_so_shared_instance_untouched(self):
        """The pool's cached agent shares its tool refs with
        every spawn — mutating those would race other spawns.
        After rebind, the agent's tool list contains DIFFERENT
        instances from the originals."""
        a = _FakeTool(Path("/orig/a"))
        agent = MagicMock()
        agent.tools = [a]

        OrchestrateTools._rebind_tool_base_dirs(agent, Path("/wt"))
        # Original tool's base_dir is unchanged.
        assert a.base_dir == Path("/orig/a")
        # But agent now points at a copy with the new base_dir.
        assert agent.tools[0] is not a
        assert agent.tools[0].base_dir == Path("/wt")

    def test_skips_tools_without_base_dir(self):
        """Toolkits without ``base_dir`` (MCP clients, etc.) are
        documented as "still see project root". The rebind must
        not crash on them; they simply aren't tracked in the
        returned originals dict."""
        good = _FakeTool(Path("/orig"))
        bare = MagicMock(spec=[])  # no attributes at all
        agent = MagicMock()
        agent.tools = [good, bare]

        originals = OrchestrateTools._rebind_tool_base_dirs(agent, Path("/wt"))
        assert len(originals) == 1
        # Tool that DID have base_dir is rebound.
        rebased_good = next(iter(originals))
        assert rebased_good.base_dir == Path("/wt")

    def test_no_tools_attribute_is_no_op(self):
        agent = MagicMock(spec=[])  # no .tools
        originals = OrchestrateTools._rebind_tool_base_dirs(agent, Path("/wt"))
        assert originals == {}


# ── _create_isolated_worktree ─────────────────────────────────


class TestCreateIsolatedWorktree:
    def test_returns_error_when_no_project_dir(self):
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings())
        manager, payload = tool._create_isolated_worktree("editor")
        assert manager is None
        assert "requires a project" in payload

    def test_returns_error_when_not_a_git_repo(self, tmp_path):
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        manager, payload = tool._create_isolated_worktree("editor")
        assert manager is None
        assert "Error" in payload
        assert "Not a git repository" in payload or "cannot create worktree" in payload

    def test_creates_worktree_in_git_repo(self, tmp_path):
        _init_git_repo(tmp_path)
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        manager, info = tool._create_isolated_worktree("editor")
        try:
            assert manager is not None
            assert info.worktree_path.is_dir()
            assert "editor" in info.branch_name
        finally:
            if manager:
                manager.cleanup()


# ── End-to-end spawn_agent(isolation="worktree") ──────────────


class TestSpawnAgentIsolation:
    @pytest.mark.asyncio
    async def test_unknown_isolation_mode_returns_error(self, tmp_path):
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("task", "editor", isolation="docker")
        assert "Error" in result
        assert "isolation" in result.lower()

    @pytest.mark.asyncio
    async def test_isolation_without_project_dir_errors(self):
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings())
        result = await tool.spawn_agent("task", "editor", isolation="worktree")
        assert "Error" in result
        assert "project directory" in result

    @pytest.mark.asyncio
    async def test_isolation_non_git_repo_errors(self, tmp_path):
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("task", "editor", isolation="worktree")
        assert "Error" in result
        # The error should reference the underlying git failure so
        # the user / agent can act on it.
        assert "git" in result.lower() or "worktree" in result.lower()

    @pytest.mark.asyncio
    async def test_isolation_creates_and_reaps_clean_worktree(self, tmp_path):
        """Happy path: the agent doesn't touch the worktree, so
        ``manager.cleanup()`` reaps it and the footer says so."""
        _init_git_repo(tmp_path)
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("Fix bug", "editor", isolation="worktree")
        assert "agent response" in result
        assert "Worktree" in result
        assert "reaped" in result

    @pytest.mark.asyncio
    async def test_isolation_preserves_dirty_worktree(self, tmp_path):
        """When the spawned agent leaves changes in the worktree,
        the cleanup step preserves it and the footer surfaces the
        merge / remove commands. Simulated here by having the
        ``_mock_stream`` agent write a file via a fake tool that
        has ``base_dir`` honoring rebind."""
        _init_git_repo(tmp_path)

        # A tool that, when its method runs, writes a file into
        # its ``base_dir`` — so we can see the worktree change.
        class _WriterTool(_FakeTool):
            def write(self, payload: str) -> str:
                (self.base_dir / "dirt.txt").write_text(payload)
                return "ok"

        pool = _mock_pool(agent_tools=[_WriterTool(tmp_path)])
        tool = OrchestrateTools(pool=pool, settings=_settings(), project_dir=tmp_path)

        # We can't realistically have the mocked agent invoke its
        # tool, so write directly via the post-rebind tool copy
        # AFTER spawn_agent has rebound base_dirs. To exercise the
        # preserved-worktree path, write through the rebased tool
        # by hooking _run_agent_streaming.
        from ember_code.core.tools import orchestrate as orch_mod

        async def _fake_run_agent_streaming(agent, *_a, **_kw):
            # Use the agent's (already-rebound) writer tool.
            writer = next(t for t in agent.tools if hasattr(t, "write"))
            writer.write("hello from isolated agent")
            return "did it", []

        original = orch_mod._run_agent_streaming
        orch_mod._run_agent_streaming = _fake_run_agent_streaming
        try:
            result = await tool.spawn_agent("Write a file", "editor", isolation="worktree")
        finally:
            orch_mod._run_agent_streaming = original

        assert "Worktree preserved" in result
        assert "git merge" in result
        # Manual cleanup so test artifacts don't pile up.
        branch_name = result.split("branch: ", 1)[1].split(")", 1)[0]
        subprocess.run(
            ["git", "worktree", "remove", "--force", branch_name],
            cwd=tmp_path,
            capture_output=True,
        )

    @pytest.mark.asyncio
    async def test_isolation_prepends_worktree_note_to_task(self, tmp_path):
        """The agent receives a task body that begins with a
        description of the worktree path / branch so the model
        knows where to operate even for tools that lack
        ``base_dir`` rebinding."""
        _init_git_repo(tmp_path)
        from ember_code.core.tools import orchestrate as orch_mod

        captured: dict = {}

        async def _capturing_run(_agent, task, *_a, **_kw):
            captured["task"] = task
            return "ok", []

        original = orch_mod._run_agent_streaming
        orch_mod._run_agent_streaming = _capturing_run
        try:
            tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
            await tool.spawn_agent("Original task", "editor", isolation="worktree")
        finally:
            orch_mod._run_agent_streaming = original

        body = captured["task"]
        assert "isolated git worktree" in body
        assert "Original task" in body  # original task is preserved verbatim

    @pytest.mark.asyncio
    async def test_isolation_restores_base_dirs_after_run(self, tmp_path):
        """The pool's shared tools must not be left pointing at
        the worktree after the spawn ends — the rebind happens on
        shallow-copied tool instances, and even those copies are
        restored to original ``base_dir`` so a follow-up spawn
        sees the right cwd."""
        _init_git_repo(tmp_path)
        original_path = tmp_path / "original"
        original_path.mkdir()
        writer = _FakeTool(original_path)
        pool = _mock_pool(agent_tools=[writer])

        # Keep a ref to the agent so we can inspect post-spawn.
        agents_seen: list = []

        from ember_code.core.tools import orchestrate as orch_mod

        async def _capture_agent(agent, *_a, **_kw):
            agents_seen.append(agent)
            return "ok", []

        original_run = orch_mod._run_agent_streaming
        orch_mod._run_agent_streaming = _capture_agent
        try:
            tool = OrchestrateTools(pool=pool, settings=_settings(), project_dir=tmp_path)
            await tool.spawn_agent("task", "editor", isolation="worktree")
        finally:
            orch_mod._run_agent_streaming = original_run

        spawned_agent = agents_seen[0]
        # The agent's tool copy was rebased to the worktree
        # DURING the run, but restored to the original AFTER.
        rebased_writer = spawned_agent.tools[0]
        assert rebased_writer.base_dir == original_path
        # And the pool's original writer was never mutated.
        assert writer.base_dir == original_path
        assert rebased_writer is not writer

    @pytest.mark.asyncio
    async def test_isolation_empty_string_is_no_op(self, tmp_path):
        """No isolation requested → no worktree, no footer.
        Validates the non-isolated regression path."""
        _init_git_repo(tmp_path)
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("Plain task", "editor")
        assert "agent response" in result
        assert "Worktree" not in result
