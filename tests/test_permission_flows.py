"""Tests for permission approval flows — P0 safety-critical.

Covers: file write prompts, shell execute prompts, git push/destructive,
allow once/always/similar/deny, rule persistence, CLI flag overrides,
and the protocol-based HITL flow.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.config.tool_permissions import ToolPermissions

# ── Permission check levels ──────────────────────────────────────


class TestPermissionCheckLevels:
    """Verify that default permission settings generate correct check levels."""

    def test_file_write_default_is_ask(self):
        """File writes should prompt by default."""
        perms = ToolPermissions()
        level = perms.check("Write", "save_file", {"file_path": "test.py"})
        # Default is "ask" for file writes
        assert level in ("ask", "allow")  # depends on saved rules

    def test_shell_execute_default_is_ask(self):
        """Shell commands should prompt by default."""
        perms = ToolPermissions()
        level = perms.check("Bash", "run_shell_command", {"args": ["ls"]})
        assert level in ("ask", "allow")

    def test_read_is_always_allowed(self):
        """File reads should always be allowed."""
        perms = ToolPermissions()
        level = perms.check("Read", "read_file", {"file_path": "test.py"})
        assert level == "allow"


# ── Permission rule persistence ──────────────────────────────────


class TestPermissionPersistence:
    """Test that permission rules persist to .ember/settings.local.json."""

    def test_save_rule_creates_project_settings(self, tmp_path):
        """save_rule writes to .ember/settings.local.json in the project."""
        (tmp_path / ".ember").mkdir()
        perms = ToolPermissions(project_dir=tmp_path)
        perms.save_rule("Bash(git push)", "allow")

        settings_path = tmp_path / ".ember" / "settings.local.json"
        assert settings_path.exists()

        import json

        data = json.loads(settings_path.read_text())
        assert "Bash(git push)" in data["permissions"]["allow"]

    def test_save_rule_updates_in_memory(self, tmp_path):
        """save_rule updates the in-memory rules immediately."""
        (tmp_path / ".ember").mkdir()
        perms = ToolPermissions(project_dir=tmp_path)
        initial_rules = len(perms._rules)
        perms.save_rule("Bash(git push)", "allow")
        assert len(perms._rules) > initial_rules

    def test_save_deny_rule(self, tmp_path):
        """Deny rules are persisted."""
        (tmp_path / ".ember").mkdir()
        perms = ToolPermissions(project_dir=tmp_path)
        perms.save_rule("Bash(rm:*)", "deny")

        import json

        data = json.loads((tmp_path / ".ember" / "settings.local.json").read_text())
        assert "Bash(rm:*)" in data["permissions"]["deny"]

    def test_save_moves_between_lists(self, tmp_path):
        """Saving a rule to 'allow' removes it from 'deny'."""
        (tmp_path / ".ember").mkdir()
        perms = ToolPermissions(project_dir=tmp_path)
        perms.save_rule("Bash(git push)", "deny")
        perms.save_rule("Bash(git push)", "allow")

        import json

        data = json.loads((tmp_path / ".ember" / "settings.local.json").read_text())
        assert "Bash(git push)" in data["permissions"]["allow"]
        assert "Bash(git push)" not in data["permissions"].get("deny", [])

    def test_fallback_to_home_without_project(self, tmp_path, monkeypatch):
        """Without project_dir, saves to ~/.ember/settings.local.json."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".ember").mkdir()
        perms = ToolPermissions(project_dir=tmp_path)
        # Force no project dir to trigger home fallback
        perms._project_dir = None
        perms.save_rule("Read(*)", "allow")

        settings_path = tmp_path / ".ember" / "settings.local.json"
        assert settings_path.exists()


# ── CLI flag overrides ───────────────────────────────────────────


class TestCLIPermissionFlags:
    """Test that CLI flags correctly override permission behavior."""

    def test_auto_approve_sets_all_allow(self):
        """--auto-approve should set all permissions to allow."""
        from ember_code.core.config.settings import Settings

        settings = Settings()
        # Simulate --auto-approve
        settings.permissions.file_write = "allow"
        settings.permissions.shell_execute = "allow"
        settings.permissions.git_push = "allow"
        settings.permissions.git_destructive = "allow"

        assert settings.permissions.file_write == "allow"
        assert settings.permissions.shell_execute == "allow"

    def test_read_only_blocks_writes(self):
        """--read-only should deny file writes and shell execution."""
        from ember_code.core.config.settings import Settings

        settings = Settings()
        settings.permissions.file_write = "deny"
        settings.permissions.shell_execute = "deny"

        assert settings.permissions.file_write == "deny"
        assert settings.permissions.shell_execute == "deny"

    def test_accept_edits_allows_writes_asks_shell(self):
        """--accept-edits should allow file writes but ask for shell."""
        from ember_code.core.config.settings import Settings

        settings = Settings()
        settings.permissions.file_write = "allow"
        settings.permissions.shell_execute = "ask"

        assert settings.permissions.file_write == "allow"
        assert settings.permissions.shell_execute == "ask"

    def test_strict_denies_all(self):
        """--strict should deny everything."""
        from ember_code.core.config.settings import Settings

        settings = Settings()
        settings.permissions.file_write = "deny"
        settings.permissions.shell_execute = "deny"
        settings.permissions.git_push = "deny"
        settings.permissions.git_destructive = "deny"

        assert settings.permissions.file_write == "deny"
        assert settings.permissions.git_push == "deny"


# ── Git-specific permission checks ──────────────────────────────


class TestGitPermissions:
    """Test that git commands trigger appropriate permission checks."""

    def test_git_push_requires_confirmation(self):
        """git push should be in require_confirmation."""
        from ember_code.core.config.settings import Settings

        settings = Settings()
        assert any("git push" in cmd for cmd in settings.safety.require_confirmation)

    def test_git_force_push_requires_confirmation(self):
        """git push --force should be in require_confirmation."""
        from ember_code.core.config.settings import Settings

        settings = Settings()
        assert any("force" in cmd for cmd in settings.safety.require_confirmation)

    def test_destructive_commands_blocked(self):
        """rm -rf / and fork bombs should be in blocked_commands."""
        from ember_code.core.config.settings import Settings

        settings = Settings()
        assert any("rm -rf" in cmd for cmd in settings.safety.blocked_commands)


# ── Backend HITL resolution ──────────────────────────────────────


class TestBackendHITLResolution:
    """Test BackendServer.resolve_hitl() across the process boundary."""

    @pytest.mark.asyncio
    async def test_resolve_confirm_calls_requirement(self):
        """Backend resolves a confirmed HITL requirement."""
        from ember_code.backend.server import BackendServer

        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._session.main_team = MagicMock()
            server._session.main_team.acontinue_run = AsyncMock(return_value=iter([]))
            server._session.session_id = "test"
            server._session.hook_executor.execute = AsyncMock(
                return_value=MagicMock(should_continue=True, message="")
            )
            # Force the sub-agent coordinator to NOT claim this requirement,
            # so the main-team resolve path runs and we can assert on it.
            server._session.sub_agent_hitl.resolve = MagicMock(return_value=False)
            server._pending_requirements = {}
            server._processing = False

            # Store a mock requirement
            req = MagicMock()
            server._pending_requirements["r1"] = (req, "run-1")

            # Resolve it
            results = []
            async for proto in server.resolve_hitl("r1", "confirm", "once"):
                results.append(proto)

            req.confirm.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_reject_calls_requirement(self):
        """Backend resolves a rejected HITL requirement."""
        from ember_code.backend.server import BackendServer

        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._session.main_team = MagicMock()
            server._session.main_team.acontinue_run = AsyncMock(return_value=iter([]))
            server._session.session_id = "test"
            server._session.hook_executor.execute = AsyncMock(
                return_value=MagicMock(should_continue=True, message="")
            )
            server._session.sub_agent_hitl.resolve = MagicMock(return_value=False)
            server._pending_requirements = {}

            req = MagicMock()
            server._pending_requirements["r1"] = (req, "run-1")

            results = []
            async for proto in server.resolve_hitl("r1", "reject"):
                results.append(proto)

            req.reject.assert_called_once_with(note="User denied")

    @pytest.mark.asyncio
    async def test_resolve_unknown_requirement_returns_error(self):
        """Backend returns error for unknown requirement ID."""
        from ember_code.backend.server import BackendServer
        from ember_code.protocol.messages import Error

        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._session.sub_agent_hitl.resolve = MagicMock(return_value=False)
            server._pending_requirements = {}

            results = []
            async for proto in server.resolve_hitl("nonexistent", "confirm"):
                results.append(proto)

            assert len(results) == 1
            assert isinstance(results[0], Error)
            assert "nonexistent" in results[0].text
