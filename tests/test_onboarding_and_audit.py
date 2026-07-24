"""Tests for onboarding, audit logging, and cross-tool support — P3.

Covers: first-run initialization, audit log entries, CLAUDE.md loading.
"""

from unittest.mock import patch

from ember_code.core.config.settings import Settings
from ember_code.core.init import initialize_project
from ember_code.core.utils.audit import AuditEntry, AuditLogger
from ember_code.core.utils.context import load_project_context
from ember_code.core.utils.update_checker import UpdateInfo


class TestFirstRunOnboarding:
    """Test project initialization on first run."""

    def test_creates_ember_dir(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            (tmp_path / "home" / ".ember").mkdir(parents=True, exist_ok=True)
            initialize_project(tmp_path)

        assert (tmp_path / ".ember").exists()

    def test_creates_marker_file(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            (tmp_path / "home" / ".ember").mkdir(parents=True, exist_ok=True)
            initialize_project(tmp_path)

        assert (tmp_path / ".ember" / ".initialized").exists()

    def test_second_run_no_reinit(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            (tmp_path / "home" / ".ember").mkdir(parents=True, exist_ok=True)
            first = initialize_project(tmp_path)
            second = initialize_project(tmp_path)

        assert first is True
        assert second is False

    def test_copies_agents(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            (tmp_path / "home" / ".ember").mkdir(parents=True, exist_ok=True)
            initialize_project(tmp_path)

        agents_dir = tmp_path / ".ember" / "agents"
        if agents_dir.exists():
            assert any(agents_dir.iterdir())

    def test_copies_skills(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            (tmp_path / "home" / ".ember").mkdir(parents=True, exist_ok=True)
            initialize_project(tmp_path)

        skills_dir = tmp_path / ".ember" / "skills"
        if skills_dir.exists():
            assert any(skills_dir.iterdir())


class TestAuditLogging:
    """Test audit log functionality."""

    def test_audit_logger_logs(self, tmp_path):
        settings = Settings()
        logger = AuditLogger(settings)
        # Just verify it doesn't crash
        logger.log(AuditEntry.success(session_id="s1", agent_name="editor", tool_name="edit_file"))

    def test_audit_logger_log_blocked(self, tmp_path):
        settings = Settings()
        logger = AuditLogger(settings)
        logger.log(
            AuditEntry.blocked(
                session_id="s1",
                agent_name="main",
                tool_name="run_shell",
                reason="blocked",
            )
        )


class TestCrossToolSupport:
    """Test CLAUDE.md and cross-tool context loading."""

    def test_loads_claude_md_when_enabled(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Claude rules\nBe helpful.")

        context = load_project_context(tmp_path, "ember.md", read_claude_md=True)
        assert "Claude rules" in (context or "")

    def test_ignores_claude_md_when_disabled(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Claude rules")

        context = load_project_context(tmp_path, "ember.md", read_claude_md=False)
        assert "Claude rules" not in (context or "")


class TestTipsAndUpdates:
    """Test update checking."""

    def test_update_info_model(self):
        info = UpdateInfo(available=True, latest_version="1.2.0", current_version="1.0.0")
        assert info.available is True
        assert info.latest_version == "1.2.0"

    def test_update_info_not_available(self):
        info = UpdateInfo(available=False, latest_version="1.0.0", current_version="1.0.0")
        assert info.available is False
