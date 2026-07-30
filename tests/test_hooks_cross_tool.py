"""Tests for hooks cross_tool_support — loading hooks from .claude/ paths."""

import json
from pathlib import Path
from unittest.mock import patch

from ember_code.core.hooks.loader import HookLoader


class TestHooksCrossToolSupport:
    """When cross_tool_support=True, hooks load from .claude/ directories too."""

    def _make_settings_file(self, path: Path, hooks: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": hooks}))

    def test_loads_claude_project_settings(self, tmp_path):
        """Loads hooks from .claude/settings.json when cross_tool_support=True."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        claude_dir = tmp_path / ".claude"
        self._make_settings_file(
            claude_dir / "settings.json",
            {"PreToolUse": [{"type": "command", "command": "echo claude"}]},
        )

        with patch.object(Path, "home", return_value=fake_home):
            loader = HookLoader(tmp_path, cross_tool_support=True)
            hooks = loader.load().registry.raw

        assert "PreToolUse" in hooks
        assert hooks["PreToolUse"][0].command == "echo claude"

    def test_does_not_load_claude_when_disabled(self, tmp_path):
        """Does NOT load .claude/ hooks when cross_tool_support=False."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        claude_dir = tmp_path / ".claude"
        self._make_settings_file(
            claude_dir / "settings.json",
            {"PreToolUse": [{"type": "command", "command": "echo claude"}]},
        )

        with patch.object(Path, "home", return_value=fake_home):
            loader = HookLoader(tmp_path, cross_tool_support=False)
            hooks = loader.load().registry.raw

        assert hooks == {}

    def test_loads_claude_user_settings(self, tmp_path):
        """Loads hooks from ~/.claude/settings.json when cross_tool_support=True."""
        fake_home = tmp_path / "home"
        self._make_settings_file(
            fake_home / ".claude" / "settings.json",
            {"Stop": [{"type": "command", "command": "echo user-claude"}]},
        )

        with patch.object(Path, "home", return_value=fake_home):
            loader = HookLoader(tmp_path, cross_tool_support=True)
            hooks = loader.load().registry.raw

        assert "Stop" in hooks
        assert hooks["Stop"][0].command == "echo user-claude"

    def test_loads_claude_local_settings(self, tmp_path):
        """Loads hooks from .claude/settings.local.json."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        claude_dir = tmp_path / ".claude"
        self._make_settings_file(
            claude_dir / "settings.local.json",
            {"PostToolUse": [{"type": "command", "command": "echo local"}]},
        )

        with patch.object(Path, "home", return_value=fake_home):
            loader = HookLoader(tmp_path, cross_tool_support=True)
            hooks = loader.load().registry.raw

        assert "PostToolUse" in hooks

    def test_ember_hooks_merge_with_claude_hooks(self, tmp_path):
        """Ember and Claude hooks merge (both loaded)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        # Ember hook
        ember_dir = tmp_path / ".ember"
        self._make_settings_file(
            ember_dir / "settings.json",
            {"PreToolUse": [{"type": "command", "command": "echo ember"}]},
        )

        # Claude hook (different event)
        claude_dir = tmp_path / ".claude"
        self._make_settings_file(
            claude_dir / "settings.json",
            {"Stop": [{"type": "command", "command": "echo claude"}]},
        )

        with patch.object(Path, "home", return_value=fake_home):
            loader = HookLoader(tmp_path, cross_tool_support=True)
            hooks = loader.load().registry.raw

        assert "PreToolUse" in hooks
        assert "Stop" in hooks

    def test_same_event_hooks_accumulate(self, tmp_path):
        """Same event from ember and claude both appear in hook list."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        ember_dir = tmp_path / ".ember"
        self._make_settings_file(
            ember_dir / "settings.json",
            {"PreToolUse": [{"type": "command", "command": "echo ember"}]},
        )

        claude_dir = tmp_path / ".claude"
        self._make_settings_file(
            claude_dir / "settings.json",
            {"PreToolUse": [{"type": "command", "command": "echo claude"}]},
        )

        with patch.object(Path, "home", return_value=fake_home):
            loader = HookLoader(tmp_path, cross_tool_support=True)
            hooks = loader.load().registry.raw

        assert len(hooks["PreToolUse"]) == 2
