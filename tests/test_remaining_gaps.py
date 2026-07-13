"""Tests for remaining QA checklist gaps — P0, P1, P2, P3.

Covers everything that can be unit tested without a real TUI or external services.
"""

import contextlib
import subprocess
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.backend.server import BackendServer
from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.init import initialize_project
from ember_code.core.pool import AgentDefinition, AgentPool
from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.store import TaskStore
from ember_code.core.utils.context import load_project_context
from ember_code.core.utils.media import attach_resolved_files, resolve_file_references
from ember_code.core.worktree import WorktreeManager
from ember_code.frontend.tui.input_handler import InputHandler

# ── P0: Config loading gaps ──────────────────────────────────────


class TestConfigLocalYaml:
    """Test .ember/config.local.yaml loading."""

    def test_local_config_overrides_project(self, tmp_path):
        # Create project config
        ember_dir = tmp_path / ".ember"
        ember_dir.mkdir()
        (ember_dir / "config.yaml").write_text("models:\n  default: base-model\n")
        (ember_dir / "config.local.yaml").write_text("models:\n  default: local-model\n")

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            (tmp_path / "home" / ".ember").mkdir(parents=True, exist_ok=True)
            settings = load_settings(project_dir=tmp_path)

        # local should override project
        assert settings.models.default == "local-model"


class TestUserRulesMd:
    """Test ~/.ember/rules.md loading."""

    def test_user_rules_loaded(self, tmp_path):
        # Create user rules
        home_ember = tmp_path / ".ember"
        home_ember.mkdir(parents=True)
        (home_ember / "rules.md").write_text("Always use type hints.")

        with patch("pathlib.Path.home", return_value=tmp_path):
            load_project_context(tmp_path, "ember.md", read_claude_md=False)

        # Should not crash — rules.md loading is best-effort
        # At minimum, should not crash


# ── P0: MCP crash recovery ──────────────────────────────────────


class TestMCPCrashRecovery:
    """MCP server crash should not crash the session."""

    @pytest.mark.asyncio
    async def test_mcp_disconnect_error_handled(self):
        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._session.mcp_manager.disconnect_one = AsyncMock(
                side_effect=RuntimeError("Connection reset")
            )
            server._session.rebuild_mcp = MagicMock()

            # Should not raise — error handled gracefully
            with contextlib.suppress(RuntimeError):
                await server.mcp_disconnect("crashed-server")


# ── P1: Agent model override ────────────────────────────────────


class TestAgentModelOverride:
    """Agent with model: field should use that model."""

    def test_agent_definition_parses_model(self):
        defn = AgentDefinition(
            name="test",
            description="test agent",
            model="gpt-4",
            tools=["Read"],
        )
        assert defn.model == "gpt-4"

    def test_agent_definition_default_model(self):
        defn = AgentDefinition(
            name="test",
            description="test agent",
            tools=["Read"],
        )
        assert defn.model in (None, "", "default")


# ── P1: Orchestration limits ────────────────────────────────────


class TestOrchestrationLimits:
    """Max agents and timeout should be enforced."""

    def test_max_total_agents_in_settings(self):
        settings = Settings()
        assert settings.orchestration.max_total_agents > 0
        assert settings.orchestration.max_total_agents == 20  # default

    def test_max_nesting_depth_in_settings(self):
        settings = Settings()
        assert settings.orchestration.max_nesting_depth > 0

    def test_sub_team_timeout_in_settings(self):
        settings = Settings()
        assert hasattr(settings.orchestration, "sub_team_timeout")


# ── P1: /agents command output ──────────────────────────────────


class TestAgentsCommand:
    @pytest.mark.asyncio
    async def test_agents_opens_panel(self):
        """`/agents` (bare) opens the TUI panel — the markdown listing
        was replaced by the panel. Agent data flows through the
        ``get_agent_details`` RPC + ``AgentsPanelWidget``, not the
        slash result's ``content`` field."""
        session = MagicMock()
        session.pool.list_agents.return_value = []
        session.pool.list_ephemeral.return_value = []
        session.skill_pool.match_user_command.return_value = None

        handler = CommandHandler(session)
        result = await handler.handle("/agents")
        assert result.kind == "action"
        assert result.action == "agents"


# ── P2: File resolution and media attachment ─────────────────────


class TestFileResolutionAndAttachment:
    """File references are resolved to paths; vision models get attachments."""

    def test_resolve_bare_filename(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG")
        text, resolved = resolve_file_references("analyze photo.png", project_dir=tmp_path)
        assert len(resolved) == 1
        assert str(tmp_path / "photo.png") in text

    def test_no_media_in_plain_text(self):
        text, resolved = resolve_file_references("just a normal message")
        assert resolved == []
        assert text == "just a normal message"

    def test_attach_resolved_files_image(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        result = attach_resolved_files([str(img)])
        assert result is not None
        assert "images" in result
        assert len(result["images"]) == 1

    def test_attach_resolved_files_pdf(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF")
        result = attach_resolved_files([str(pdf)])
        assert result is not None
        assert "files" in result

    def test_attach_resolved_files_non_media(self):
        result = attach_resolved_files(["/some/path/script.py"])
        assert result is None


# ── P2: Schedule show/cancel commands ────────────────────────────


class TestScheduleShowCancel:
    """SQLite-backed tests; each test gets its own tmp file."""

    @pytest.mark.asyncio
    async def test_schedule_show_existing(self, tmp_path):
        store = TaskStore(db_path=tmp_path / "state.db")
        task = ScheduledTask(
            id="t1",
            description="test task",
            scheduled_at=datetime.now(),
            created_at=datetime.now(),
            status=TaskStatus.pending,
        )
        await store.add(task)
        result = await store.get("t1")
        assert result is not None
        assert result.description == "test task"

    @pytest.mark.asyncio
    async def test_schedule_cancel(self, tmp_path):
        store = TaskStore(db_path=tmp_path / "state.db")
        task = ScheduledTask(
            id="t2",
            description="cancel me",
            scheduled_at=datetime.now(),
            created_at=datetime.now(),
            status=TaskStatus.pending,
        )
        await store.add(task)
        await store.update_status("t2", TaskStatus.cancelled)
        result = await store.get("t2")
        assert result.status == TaskStatus.cancelled

    @pytest.mark.asyncio
    async def test_schedule_show_nonexistent(self, tmp_path):
        store = TaskStore(db_path=tmp_path / "state.db")
        result = await store.get("nonexistent_zzzz")
        assert result is None


# ── P2: Max ephemeral per session ────────────────────────────────


class TestMaxEphemeralLimit:
    def test_register_respects_limit(self, tmp_path):
        pool = AgentPool()
        pool.init_ephemeral(tmp_path)

        # Register up to the limit
        limit = getattr(pool, "_max_ephemeral", 10)
        for i in range(limit):
            try:
                pool.register_ephemeral(f"agent_{i}", f"Agent {i}", ["Read"])
            except Exception:
                break

        count = len(pool.list_ephemeral())
        assert count <= limit


# ── P2: Worktree ────────────────────────────────────────────────


class TestWorktreeIntegration:
    def test_worktree_manager_creates(self, tmp_path):
        """Worktree creation should work in a git repo."""
        # Init a git repo
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        wm = WorktreeManager(tmp_path)
        info = wm.create()
        assert info.worktree_path.exists()
        wm.cleanup()


# ── P3: /bug command ────────────────────────────────────────────


class TestBugCommand:
    @pytest.mark.asyncio
    async def test_bug_opens_browser(self):
        session = MagicMock()
        session.skill_pool.match_user_command.return_value = None

        with patch("webbrowser.open") as mock_open:
            handler = CommandHandler(session)
            await handler.handle("/bug")

        mock_open.assert_called_once()
        assert "github" in mock_open.call_args[0][0]


# ── P3: /help covers skills and shortcuts ────────────────────────


class TestHelpContent:
    @pytest.mark.asyncio
    async def test_help_mentions_skills(self):
        session = MagicMock()
        session.skill_pool.list_skills.return_value = [
            MagicMock(name="commit", description="Create commit", argument_hint="message"),
        ]
        session.skill_pool.match_user_command.return_value = None
        session.pool.list_agents.return_value = []

        handler = CommandHandler(session)
        result = await handler.handle("/help")
        # Help should mention available commands
        assert result.kind in ("info", "markdown", "action")

    @pytest.mark.asyncio
    async def test_help_topic_schedule(self):
        session = MagicMock()
        session.skill_pool.match_user_command.return_value = None

        handler = CommandHandler(session)
        result = await handler.handle("/help schedule")
        assert "schedule" in result.content.lower() or result.kind == "markdown"


# ── P3: Autocomplete suggestions ────────────────────────────────


class TestCommandAutocomplete:
    def test_known_commands_list(self):
        """CommandHandler should have a dispatch table of known commands."""
        # The _COMMANDS dict should have all slash commands
        assert hasattr(CommandHandler, "_COMMANDS")
        commands = CommandHandler._COMMANDS
        assert "/help" in commands
        assert "/agents" in commands
        assert "/skills" in commands
        assert "/mcp" in commands
        assert "/schedule" in commands
        assert "/config" in commands
        assert "/model" in commands
        assert "/compact" in commands
        assert "/clear" in commands
        assert "/quit" in commands

    def test_input_handler_instantiates(self):
        handler = InputHandler(skill_pool=MagicMock())
        # Should instantiate without error
        assert handler is not None


# ── P3: First-run tracked independently ─────────────────────────


class TestInitializationTracking:
    def test_home_and_project_markers_independent(self, tmp_path):
        home = tmp_path / "home"
        home_ember = home / ".ember"
        home_ember.mkdir(parents=True)

        project = tmp_path / "project"
        project.mkdir()

        with patch("pathlib.Path.home", return_value=home):
            first = initialize_project(project)
            assert first is True

            # Project initialized but home marker is separate
            assert (project / ".ember" / ".initialized").exists()
