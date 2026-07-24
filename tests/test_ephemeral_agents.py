"""Tests for ephemeral agent lifecycle — register, list, promote, limits."""

from unittest.mock import MagicMock, patch

import pytest

from ember_code.core.agents import AgentPool
from ember_code.core.tools.orchestrate import OrchestrateTools


class TestEphemeralInit:
    """AgentPool.init_ephemeral() sets up the temp directory."""

    def test_creates_agents_tmp_dir(self, tmp_path, settings):
        pool = AgentPool()
        pool.init_ephemeral(tmp_path)
        assert (tmp_path / ".ember" / "agents.tmp").is_dir()

    def test_idempotent(self, tmp_path, settings):
        pool = AgentPool()
        pool.init_ephemeral(tmp_path)
        pool.init_ephemeral(tmp_path)  # no error
        assert (tmp_path / ".ember" / "agents.tmp").is_dir()


class TestRegisterEphemeral:
    """AgentPool.register_ephemeral() creates and registers temp agents."""

    @pytest.fixture
    def pool(self, tmp_path, settings):
        p = AgentPool()
        p._settings = settings
        p._base_dir = str(tmp_path)
        p.init_ephemeral(tmp_path)
        return p

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_register_creates_md_file(self, mock_build, pool, tmp_path):
        mock_build.return_value = MagicMock()
        pool.register_ephemeral(
            name="helper",
            description="A helper agent",
            system_prompt="You help with things.",
        )
        md_path = tmp_path / ".ember" / "agents.tmp" / "helper.md"
        assert md_path.exists()
        content = md_path.read_text()
        assert "name: helper" in content
        assert "description: A helper agent" in content
        assert "You help with things." in content

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_register_adds_to_definitions(self, mock_build, pool):
        mock_build.return_value = MagicMock()
        pool.register_ephemeral(
            name="helper",
            description="A helper",
            system_prompt="Help.",
        )
        assert "helper" in pool.agent_names

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_register_with_custom_tools(self, mock_build, pool, tmp_path):
        mock_build.return_value = MagicMock()
        pool.register_ephemeral(
            name="searcher",
            description="Searches code",
            system_prompt="Search.",
            tools=["Read", "Grep", "Glob"],
        )
        md_path = tmp_path / ".ember" / "agents.tmp" / "searcher.md"
        content = md_path.read_text()
        assert "Read, Grep, Glob" in content

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_register_with_model(self, mock_build, pool, tmp_path):
        mock_build.return_value = MagicMock()
        pool.register_ephemeral(
            name="smart",
            description="Smart agent",
            system_prompt="Be smart.",
            model="gpt-4o",
        )
        md_path = tmp_path / ".ember" / "agents.tmp" / "smart.md"
        content = md_path.read_text()
        assert "model: gpt-4o" in content

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_register_duplicate_raises(self, mock_build, pool):
        mock_build.return_value = MagicMock()
        pool.register_ephemeral(name="dup", description="d", system_prompt="p")
        with pytest.raises(ValueError, match="already exists"):
            pool.register_ephemeral(name="dup", description="d2", system_prompt="p2")

    def test_register_without_init_raises(self, settings):
        pool = AgentPool()
        pool._settings = settings
        with pytest.raises(RuntimeError, match="not initialized"):
            pool.register_ephemeral(name="x", description="d", system_prompt="p")

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_register_respects_limit(self, mock_build, pool):
        mock_build.return_value = MagicMock()
        pool._max_ephemeral = 2
        pool.register_ephemeral(name="a1", description="d", system_prompt="p")
        pool.register_ephemeral(name="a2", description="d", system_prompt="p")
        with pytest.raises(ValueError, match="limit reached"):
            pool.register_ephemeral(name="a3", description="d", system_prompt="p")


class TestListEphemeral:
    """AgentPool.list_ephemeral() returns only ephemeral agent definitions."""

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_list_ephemeral_agents(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.register_ephemeral(name="eph1", description="d", system_prompt="p")

        # Also add a non-ephemeral agent
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "perm.md").write_text(
            "---\nname: perm\ndescription: Permanent\n---\nPermanent agent.\n"
        )
        pool.load_directory(agents_dir, priority=0, settings=settings)

        ephemeral = pool.list_ephemeral()
        assert len(ephemeral) == 1
        assert ephemeral[0].name == "eph1"

    def test_list_ephemeral_without_init(self):
        pool = AgentPool()
        assert pool.list_ephemeral() == []


class TestPromoteEphemeral:
    """AgentPool.promote_ephemeral() moves agent from tmp to permanent."""

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_promote_moves_file(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.register_ephemeral(name="promo", description="d", system_prompt="p")
        dest = pool.promote_ephemeral("promo", tmp_path)

        assert dest == tmp_path / ".ember" / "agents" / "promo.md"
        assert dest.exists()
        assert not (tmp_path / ".ember" / "agents.tmp" / "promo.md").exists()

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_promote_decrements_count(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.register_ephemeral(name="promo", description="d", system_prompt="p")
        assert pool._ephemeral_count == 1
        pool.promote_ephemeral("promo", tmp_path)
        assert pool._ephemeral_count == 0

    def test_promote_nonexistent_raises(self, tmp_path, settings):
        pool = AgentPool()
        pool._settings = settings
        pool.init_ephemeral(tmp_path)
        with pytest.raises(KeyError, match="not found"):
            pool.promote_ephemeral("ghost", tmp_path)

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_promote_non_ephemeral_raises(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        # Add a permanent agent
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "perm.md").write_text(
            "---\nname: perm\ndescription: Permanent\n---\nPermanent.\n"
        )
        pool.load_directory(agents_dir, priority=0, settings=settings)

        with pytest.raises(ValueError, match="not an ephemeral"):
            pool.promote_ephemeral("perm", tmp_path)


class TestDiscardEphemeral:
    """AgentPool.discard_ephemeral() deletes an ephemeral agent."""

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_discard_removes_file_and_definition(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.register_ephemeral(name="temp", description="d", system_prompt="p")
        md_path = tmp_path / ".ember" / "agents.tmp" / "temp.md"
        assert md_path.exists()
        assert "temp" in pool.agent_names

        pool.discard_ephemeral("temp")
        assert not md_path.exists()
        assert "temp" not in pool.agent_names

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_discard_decrements_count(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.register_ephemeral(name="temp", description="d", system_prompt="p")
        assert pool._ephemeral_count == 1
        pool.discard_ephemeral("temp")
        assert pool._ephemeral_count == 0

    def test_discard_nonexistent_raises(self, tmp_path, settings):
        pool = AgentPool()
        pool._settings = settings
        pool.init_ephemeral(tmp_path)
        with pytest.raises(KeyError, match="not found"):
            pool.discard_ephemeral("ghost")

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_discard_non_ephemeral_raises(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "perm.md").write_text(
            "---\nname: perm\ndescription: Permanent\n---\nPermanent.\n"
        )
        pool.load_directory(agents_dir, priority=0, settings=settings)

        with pytest.raises(ValueError, match="not an ephemeral"):
            pool.discard_ephemeral("perm")

    def test_discard_without_init_raises(self, settings):
        pool = AgentPool()
        pool._settings = settings
        with pytest.raises(RuntimeError, match="not initialized"):
            pool.discard_ephemeral("x")


class TestCleanupEphemeral:
    """AgentPool.cleanup_ephemeral() removes all ephemeral agents."""

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_cleanup_removes_all(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.register_ephemeral(name="e1", description="d", system_prompt="p")
        pool.register_ephemeral(name="e2", description="d", system_prompt="p")
        assert pool._ephemeral_count == 2

        removed = pool.cleanup_ephemeral()
        assert removed == 2
        assert pool._ephemeral_count == 0
        assert not (tmp_path / ".ember" / "agents.tmp" / "e1.md").exists()
        assert not (tmp_path / ".ember" / "agents.tmp" / "e2.md").exists()
        assert "e1" not in pool.agent_names
        assert "e2" not in pool.agent_names

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_cleanup_leaves_permanent_agents(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        pool.register_ephemeral(name="eph", description="d", system_prompt="p")

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "perm.md").write_text(
            "---\nname: perm\ndescription: Permanent\n---\nPermanent.\n"
        )
        pool.load_directory(agents_dir, priority=0, settings=settings)

        removed = pool.cleanup_ephemeral()
        assert removed == 1
        assert "perm" in pool.agent_names

    def test_cleanup_without_init_returns_zero(self):
        pool = AgentPool()
        assert pool.cleanup_ephemeral() == 0


class TestCreateAgentTool:
    """OrchestrateTools.create_agent() creates ephemeral agents at runtime."""

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_create_agent_returns_confirmation(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path)

        tools = OrchestrateTools(
            pool=pool,
            settings=settings,
        )
        # Manually register create_agent (normally gated by settings)
        tools.register(tools.create_agent)

        result = tools.create_agent(
            name="debugger",
            description="Debugs issues",
            system_prompt="You debug code.",
        )
        assert "Created ephemeral agent" in result
        assert "debugger" in result
        assert "debugger" in pool.agent_names

    @patch("ember_code.core.agents.builder.AgentBuilder.build")
    def test_create_agent_error_on_limit(self, mock_build, tmp_path, settings):
        mock_build.return_value = MagicMock()
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(tmp_path)
        pool.init_ephemeral(tmp_path, max_ephemeral=0)

        tools = OrchestrateTools(pool=pool, settings=settings)

        result = tools.create_agent(
            name="x",
            description="d",
            system_prompt="p",
        )
        assert "Error" in result
