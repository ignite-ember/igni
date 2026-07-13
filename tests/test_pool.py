"""Tests for pool.py — agent parsing and pool management."""

from unittest.mock import MagicMock, patch

import pytest

from ember_code.core.pool import AgentDefinition, AgentPool, build_agent, parse_agent_file
from ember_code.core.pool import AgentPriority


class TestAgentParser:
    def test_parse_valid_md(self, sample_agent_md):
        defn = parse_agent_file(sample_agent_md)
        assert defn.name == "test-agent"
        assert defn.description == "A test agent"
        assert defn.tools == ["Read", "Grep"]
        assert defn.model == "MiniMax-M2.7"
        assert defn.tags == ["test", "example"]
        assert defn.reasoning is True
        assert defn.reasoning_min_steps == 2
        assert defn.reasoning_max_steps == 8
        assert "test agent" in defn.system_prompt

    def test_parse_missing_frontmatter(self, tmp_path):
        md = tmp_path / "bad.md"
        md.write_text("No frontmatter here\n")
        with pytest.raises(ValueError, match="No YAML frontmatter"):
            parse_agent_file(md)

    def test_parse_missing_name(self, tmp_path):
        md = tmp_path / "noname.md"
        md.write_text("---\ndescription: test\n---\nbody\n")
        with pytest.raises(ValueError, match="missing 'name'"):
            parse_agent_file(md)

    def test_parse_missing_description(self, tmp_path):
        md = tmp_path / "nodesc.md"
        md.write_text("---\nname: test\n---\nbody\n")
        with pytest.raises(ValueError, match="missing 'description'"):
            parse_agent_file(md)

    def test_parse_tools_as_string(self, tmp_path):
        md = tmp_path / "tools-str.md"
        md.write_text("---\nname: t\ndescription: d\ntools: Read, Write, Edit\n---\n")
        defn = parse_agent_file(md)
        assert defn.tools == ["Read", "Write", "Edit"]

    def test_parse_tools_as_list(self, tmp_path):
        md = tmp_path / "tools-list.md"
        md.write_text("---\nname: t\ndescription: d\ntools:\n  - Bash\n  - Grep\n---\n")
        defn = parse_agent_file(md)
        assert defn.tools == ["Bash", "Grep"]

    def test_parse_mcp_servers(self, tmp_path):
        md = tmp_path / "mcp.md"
        md.write_text(
            "---\nname: db\ndescription: DB agent\n"
            "tools: Read, Write\n"
            "mcp_servers: [postgres, redis]\n"
            "---\nDB agent.\n"
        )
        defn = parse_agent_file(md)
        assert defn.mcp_servers == ["postgres", "redis"]

    def test_parse_mcp_servers_absent(self, tmp_path):
        md = tmp_path / "no-mcp.md"
        md.write_text("---\nname: t\ndescription: d\n---\n")
        defn = parse_agent_file(md)
        assert defn.mcp_servers == []

    def test_parse_defaults(self, tmp_path):
        md = tmp_path / "minimal.md"
        md.write_text("---\nname: minimal\ndescription: minimal agent\n---\n")
        defn = parse_agent_file(md)
        assert defn.tools == []
        assert defn.model is None
        assert defn.reasoning is False
        assert defn.can_orchestrate is True
        assert defn.mcp_servers == []
        assert defn.temperature is None
        assert defn.max_tokens is None

    def test_parse_optional_fields(self, tmp_path):
        md = tmp_path / "full.md"
        md.write_text(
            "---\n"
            "name: full\n"
            "description: full agent\n"
            "color: blue\n"
            "can_orchestrate: false\n"
            "max_turns: 5\n"
            "temperature: 0.7\n"
            "max_tokens: 32000\n"
            "---\n"
            "Prompt body here.\n"
        )
        defn = parse_agent_file(md)
        assert defn.color == "blue"
        assert defn.can_orchestrate is False
        assert defn.max_turns == 5
        assert defn.temperature == 0.7
        # Regression: without this wire-through, the visualizer
        # sub-agent hit the provider default output cap (4-8k) and
        # its tool_call arguments got truncated mid-stream ("JSON
        # getting cut off" — the model retries in a single line and
        # loses the same way). The frontmatter override is what
        # gives it headroom.
        assert defn.max_tokens == 32000


class TestAgentPool:
    def test_empty_pool(self):
        pool = AgentPool()
        assert pool.agent_names == []
        assert pool.list_agents() == []

    def test_load_directory(self, tmp_path, settings):
        # Create agent files
        (tmp_path / "alpha.md").write_text(
            "---\nname: alpha\ndescription: Agent Alpha\n---\nAlpha prompt\n"
        )
        (tmp_path / "beta.md").write_text(
            "---\nname: beta\ndescription: Agent Beta\n---\nBeta prompt\n"
        )

        pool = AgentPool()
        pool.load_directory(tmp_path, priority=0, settings=settings)
        assert sorted(pool.agent_names) == ["alpha", "beta"]

    def test_priority_override(self, tmp_path, settings):
        low_dir = tmp_path / "low"
        low_dir.mkdir()
        (low_dir / "agent.md").write_text(
            "---\nname: shared\ndescription: Low priority\n---\nLow\n"
        )

        high_dir = tmp_path / "high"
        high_dir.mkdir()
        (high_dir / "agent.md").write_text(
            "---\nname: shared\ndescription: High priority\n---\nHigh\n"
        )

        pool = AgentPool()
        pool.load_directory(low_dir, priority=0, settings=settings)
        pool.load_directory(high_dir, priority=3, settings=settings)

        defn = pool.get_definition("shared")
        assert defn.description == "High priority"

    def test_lower_priority_does_not_override(self, tmp_path, settings):
        high_dir = tmp_path / "high"
        high_dir.mkdir()
        (high_dir / "agent.md").write_text("---\nname: shared\ndescription: High first\n---\n")

        low_dir = tmp_path / "low"
        low_dir.mkdir()
        (low_dir / "agent.md").write_text("---\nname: shared\ndescription: Low second\n---\n")

        pool = AgentPool()
        pool.load_directory(high_dir, priority=3, settings=settings)
        pool.load_directory(low_dir, priority=0, settings=settings)

        defn = pool.get_definition("shared")
        assert defn.description == "High first"

    def test_get_unknown_raises(self, settings):
        pool = AgentPool()
        with pytest.raises(KeyError, match="Agent not found"):
            pool.get("nonexistent")

    def test_load_skips_bad_files(self, tmp_path, settings):
        (tmp_path / "good.md").write_text("---\nname: good\ndescription: Works\n---\n")
        (tmp_path / "bad.md").write_text("no frontmatter here")

        pool = AgentPool()
        pool.load_directory(tmp_path, priority=0, settings=settings)
        assert pool.agent_names == ["good"]

    def test_load_nonexistent_directory(self, tmp_path, settings):
        pool = AgentPool()
        pool.load_directory(tmp_path / "nope", priority=0, settings=settings)
        assert pool.agent_names == []

    def test_describe(self, tmp_path, settings):
        (tmp_path / "agent.md").write_text(
            "---\nname: alpha\ndescription: Does things\ntools: Read, Grep\ntags: search\n---\n"
        )
        pool = AgentPool()
        pool.load_directory(tmp_path, priority=0, settings=settings)
        desc = pool.describe()
        assert "alpha" in desc
        assert "Does things" in desc
        assert "Read" in desc


class TestAgentResolutionOrder:
    """Pin down the documented resolution order so a future reorder of
    ``load_definitions`` can't silently flip who wins on a name collision.

    See the module docstring of ``pool.py`` for the canonical table.
    These tests assert: ephemeral > project Ember > project local >
    project Claude > user Ember > user Claude.
    """

    def _write_agent(self, root, name: str, label: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{name}.md").write_text(f"---\nname: {name}\ndescription: {label}\n---\nbody\n")

    def _build_layout(self, tmp_path):
        home = tmp_path / "home"
        project = tmp_path / "project"
        home.mkdir()
        project.mkdir()
        return home, project

    def _load(self, monkeypatch, home, project, settings, cross_tool=True):
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        settings.agents.cross_tool_support = cross_tool
        pool = AgentPool()
        pool.load_definitions(settings, project_dir=project)
        return pool

    def test_user_ember_beats_user_claude(self, tmp_path, monkeypatch, settings):
        home, project = self._build_layout(tmp_path)
        self._write_agent(home / ".ember" / "agents", "shared", "ember-user")
        self._write_agent(home / ".claude" / "agents", "shared", "claude-user")
        pool = self._load(monkeypatch, home, project, settings)
        assert pool.get_definition("shared").description == "ember-user"

    def test_project_local_beats_project_claude(self, tmp_path, monkeypatch, settings):
        home, project = self._build_layout(tmp_path)
        self._write_agent(project / ".ember" / "agents.local", "shared", "ember-local")
        self._write_agent(project / ".claude" / "agents", "shared", "claude-project")
        pool = self._load(monkeypatch, home, project, settings)
        assert pool.get_definition("shared").description == "ember-local"

    def test_project_ember_beats_project_claude(self, tmp_path, monkeypatch, settings):
        home, project = self._build_layout(tmp_path)
        self._write_agent(project / ".ember" / "agents", "shared", "ember-project")
        self._write_agent(project / ".claude" / "agents", "shared", "claude-project")
        pool = self._load(monkeypatch, home, project, settings)
        assert pool.get_definition("shared").description == "ember-project"

    def test_project_beats_user(self, tmp_path, monkeypatch, settings):
        home, project = self._build_layout(tmp_path)
        self._write_agent(home / ".ember" / "agents", "shared", "ember-user")
        self._write_agent(project / ".ember" / "agents", "shared", "ember-project")
        pool = self._load(monkeypatch, home, project, settings)
        assert pool.get_definition("shared").description == "ember-project"

    def test_project_claude_beats_user_ember(self, tmp_path, monkeypatch, settings):
        """Cross-scope: a project's Claude agents override the user's
        global Ember agents."""
        home, project = self._build_layout(tmp_path)
        self._write_agent(home / ".ember" / "agents", "shared", "ember-user")
        self._write_agent(project / ".claude" / "agents", "shared", "claude-project")
        pool = self._load(monkeypatch, home, project, settings)
        assert pool.get_definition("shared").description == "claude-project"

    def test_full_chain_yields_project_ember(self, tmp_path, monkeypatch, settings):
        home, project = self._build_layout(tmp_path)
        self._write_agent(home / ".claude" / "agents", "shared", "claude-user")
        self._write_agent(home / ".ember" / "agents", "shared", "ember-user")
        self._write_agent(project / ".claude" / "agents", "shared", "claude-project")
        self._write_agent(project / ".ember" / "agents.local", "shared", "ember-local")
        self._write_agent(project / ".ember" / "agents", "shared", "ember-project")
        pool = self._load(monkeypatch, home, project, settings)
        assert pool.get_definition("shared").description == "ember-project"

    def test_claude_skipped_when_cross_tool_disabled(self, tmp_path, monkeypatch, settings):
        home, project = self._build_layout(tmp_path)
        self._write_agent(project / ".claude" / "agents", "claude-only", "claude")
        pool = self._load(monkeypatch, home, project, settings, cross_tool=False)
        with pytest.raises(KeyError):
            pool.get_definition("claude-only")

    def test_priority_constants_are_ordered(self):
        assert AgentPriority.USER_CLAUDE < AgentPriority.USER_EMBER
        assert AgentPriority.USER_EMBER < AgentPriority.PROJECT_CLAUDE
        assert AgentPriority.PROJECT_CLAUDE < AgentPriority.PROJECT_LOCAL
        assert AgentPriority.PROJECT_LOCAL < AgentPriority.PROJECT_EMBER
        assert AgentPriority.PROJECT_EMBER < AgentPriority.EPHEMERAL


class TestBuildAgentMCPFiltering:
    """Tests for MCP server filtering based on agent's mcp_servers field."""

    @patch("ember_code.core.pool.Agent")
    @patch("ember_code.core.pool.ModelRegistry")
    @patch("ember_code.core.pool.ToolRegistry")
    def test_all_mcp_when_no_filter(
        self, mock_registry_cls, mock_model_cls, mock_agent_cls, settings
    ):
        mock_model_cls.return_value.get_model.return_value = MagicMock()
        mock_registry_cls.return_value.resolve.return_value = [MagicMock()]

        defn = AgentDefinition(
            name="editor",
            description="Edits files",
            tools=["Read"],
            mcp_servers=[],
        )
        pg_client = MagicMock()
        redis_client = MagicMock()
        mcp_clients = {"postgres": pg_client, "redis": redis_client}

        build_agent(defn, settings, mcp_clients=mcp_clients)
        kwargs = mock_agent_cls.call_args[1]
        assert pg_client in kwargs["tools"]
        assert redis_client in kwargs["tools"]

    @patch("ember_code.core.pool.Agent")
    @patch("ember_code.core.pool.ModelRegistry")
    @patch("ember_code.core.pool.ToolRegistry")
    def test_filtered_mcp_servers(
        self, mock_registry_cls, mock_model_cls, mock_agent_cls, settings
    ):
        mock_model_cls.return_value.get_model.return_value = MagicMock()
        mock_registry_cls.return_value.resolve.return_value = [MagicMock()]

        defn = AgentDefinition(
            name="db-agent",
            description="Database agent",
            tools=["Read"],
            mcp_servers=["postgres"],
        )
        pg_client = MagicMock()
        redis_client = MagicMock()
        mcp_clients = {"postgres": pg_client, "redis": redis_client}

        build_agent(defn, settings, mcp_clients=mcp_clients)
        kwargs = mock_agent_cls.call_args[1]
        assert pg_client in kwargs["tools"]
        assert redis_client not in kwargs["tools"]

    @patch("ember_code.core.pool.Agent")
    @patch("ember_code.core.pool.ModelRegistry")
    @patch("ember_code.core.pool.ToolRegistry")
    def test_mcp_instruction_reflects_filter(
        self, mock_registry_cls, mock_model_cls, mock_agent_cls, settings
    ):
        mock_model_cls.return_value.get_model.return_value = MagicMock()
        mock_registry_cls.return_value.resolve.return_value = [MagicMock()]

        defn = AgentDefinition(
            name="db-agent",
            description="Database agent",
            tools=["Read"],
            mcp_servers=["postgres"],
        )
        mcp_clients = {"postgres": MagicMock(), "redis": MagicMock()}

        build_agent(defn, settings, base_dir="/tmp", mcp_clients=mcp_clients)
        kwargs = mock_agent_cls.call_args[1]
        instructions_text = " ".join(kwargs["instructions"])
        assert "postgres" in instructions_text
        assert "redis" not in instructions_text

    @patch("ember_code.core.pool.Agent")
    @patch("ember_code.core.pool.ModelRegistry")
    @patch("ember_code.core.pool.ToolRegistry")
    def test_no_mcp_clients(self, mock_registry_cls, mock_model_cls, mock_agent_cls, settings):
        mock_model_cls.return_value.get_model.return_value = MagicMock()
        mock_registry_cls.return_value.resolve.return_value = [MagicMock()]

        defn = AgentDefinition(
            name="editor",
            description="Edits files",
            tools=["Read"],
            mcp_servers=["postgres"],
        )

        build_agent(defn, settings, mcp_clients=None)
        mock_agent_cls.assert_called_once()
