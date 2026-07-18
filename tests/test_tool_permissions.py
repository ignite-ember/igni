"""Tests for config/tool_permissions — permission resolution with argument rules.

Rewritten alongside the module→package refactor: the old private
helpers (``_parse_rule`` / ``_args_to_str`` / ``_extract_domain`` /
``_match_rule_args``) are gone; the equivalent behaviour now lives
on :class:`PermissionRule` (``.parse`` classmethod) and
:class:`ToolInvocationArgs` (``.primary_string`` / ``.domain``). The
tests below exercise the same behaviours through the class API.
"""

import json
from unittest.mock import patch

from ember_code.core.config.tool_permissions import (
    FUNC_TO_TOOL,
    PermissionRule,
    ToolInvocationArgs,
    ToolPermissions,
)


class TestParseRule:
    def test_bare_tool(self):
        rule = PermissionRule.parse("Bash")
        assert rule is not None
        assert rule.tool_name == "Bash"
        assert rule.arg_pattern.raw == ""

    def test_tool_with_args(self):
        rule = PermissionRule.parse("Bash(git status)")
        assert rule is not None
        assert rule.tool_name == "Bash"
        assert rule.arg_pattern.raw == "git status"

    def test_tool_with_pattern(self):
        rule = PermissionRule.parse("WebFetch(domain:github.com)")
        assert rule is not None
        assert rule.tool_name == "WebFetch"
        assert rule.arg_pattern.raw == "domain:github.com"

    def test_whitespace(self):
        rule = PermissionRule.parse("  Read  ")
        assert rule is not None
        assert rule.tool_name == "Read"
        assert rule.arg_pattern.raw == ""


class TestArgsToStr:
    def test_none(self):
        assert ToolInvocationArgs.from_dict(None).primary_string() == ""

    def test_args_list(self):
        assert (
            ToolInvocationArgs.from_dict({"args": ["git", "push"]}).primary_string() == "git push"
        )

    def test_path_key(self):
        assert (
            ToolInvocationArgs.from_dict({"path": "/src/main.py"}).primary_string()
            == "/src/main.py"
        )

    def test_file_path_key(self):
        assert ToolInvocationArgs.from_dict({"file_path": "test.py"}).primary_string() == "test.py"

    def test_fallback(self):
        result = ToolInvocationArgs.from_dict({"x": "hello", "y": "world"}).primary_string()
        assert "hello" in result
        assert "world" in result


class TestExtractDomain:
    def test_simple_url(self):
        assert (
            ToolInvocationArgs.from_dict({"url": "https://github.com/foo"}).domain() == "github.com"
        )

    def test_with_port(self):
        assert (
            ToolInvocationArgs.from_dict({"url": "http://localhost:3000/api"}).domain()
            == "localhost:3000"
        )

    def test_invalid(self):
        assert ToolInvocationArgs.from_dict({"url": "not a url"}).domain() == ""


class TestMatchRuleArgs:
    def test_exact_match(self):
        rule = PermissionRule.parse("Bash(git status)", level="allow")
        assert rule is not None
        assert rule.matches("Bash", ToolInvocationArgs.from_dict({"args": ["git", "status"]}))

    def test_prefix_wildcard(self):
        rule = PermissionRule.parse("Bash(git:*)", level="allow")
        assert rule is not None
        assert rule.matches(
            "Bash", ToolInvocationArgs.from_dict({"args": ["git", "push", "origin"]})
        )

    def test_domain_match(self):
        rule = PermissionRule.parse("WebFetch(domain:github.com)", level="allow")
        assert rule is not None
        assert rule.matches(
            "WebFetch", ToolInvocationArgs.from_dict({"url": "https://github.com/foo"})
        )

    def test_domain_mismatch(self):
        rule = PermissionRule.parse("WebFetch(domain:github.com)", level="allow")
        assert rule is not None
        assert not rule.matches(
            "WebFetch", ToolInvocationArgs.from_dict({"url": "https://evil.com"})
        )

    def test_path_match(self):
        rule = PermissionRule.parse("Read(path:src/*)", level="allow")
        assert rule is not None
        assert rule.matches("Read", ToolInvocationArgs.from_dict({"file_path": "src/main.py"}))

    def test_path_mismatch(self):
        rule = PermissionRule.parse("Read(path:src/*)", level="allow")
        assert rule is not None
        assert not rule.matches(
            "Read", ToolInvocationArgs.from_dict({"file_path": "tests/test.py"})
        )


class TestToolPermissions:
    def test_default_levels(self, tmp_path):
        perms = ToolPermissions(project_dir=tmp_path)
        assert perms.get_level("Read") == "allow"
        assert perms.get_level("Write") == "ask"
        assert perms.get_level("Bash") == "ask"
        assert perms.get_level("WebSearch") == "allow"
        assert perms.get_level("NotebookEdit") == "ask"

    def test_is_denied(self, tmp_path):
        perms = ToolPermissions(project_dir=tmp_path)
        assert not perms.is_denied("WebSearch")
        assert not perms.is_denied("Read")

    def test_needs_confirmation(self, tmp_path):
        perms = ToolPermissions(project_dir=tmp_path)
        assert perms.needs_confirmation("Bash")
        assert not perms.needs_confirmation("Read")

    def test_loads_settings_file(self, tmp_path):
        settings_dir = tmp_path / ".ember"
        settings_dir.mkdir()
        settings = {"permissions": {"allow": ["Bash"], "deny": ["Read"]}}
        (settings_dir / "settings.json").write_text(json.dumps(settings))

        perms = ToolPermissions(project_dir=tmp_path)
        assert perms.get_level("Bash") == "allow"
        assert perms.get_level("Read") == "deny"

    def test_arg_specific_rule(self, tmp_path):
        settings_dir = tmp_path / ".ember"
        settings_dir.mkdir()
        settings = {"permissions": {"allow": ["Bash(git status)"]}}
        (settings_dir / "settings.json").write_text(json.dumps(settings))

        perms = ToolPermissions(project_dir=tmp_path)
        # Arg-specific rule
        assert perms.check("Bash", tool_args={"args": ["git", "status"]}) == "allow"
        # Falls back to default for other args
        assert perms.check("Bash", tool_args={"args": ["rm", "-rf"]}) == "ask"

    def test_has_arg_rules(self, tmp_path):
        settings_dir = tmp_path / ".ember"
        settings_dir.mkdir()
        settings = {"permissions": {"allow": ["Bash(git:*)"]}}
        (settings_dir / "settings.json").write_text(json.dumps(settings))

        perms = ToolPermissions(project_dir=tmp_path)
        assert perms.has_arg_rules("Bash")
        assert not perms.has_arg_rules("Read")

    def test_save_rule(self, tmp_path):
        # Ensure ~/.ember exists for save
        home_ember = tmp_path / "home_ember"
        home_ember.mkdir()

        perms = ToolPermissions(project_dir=tmp_path)
        # ``SettingsFileWriter`` reads ``Path.home()`` when no
        # ``project_dir`` is set; the store passes project_dir, so
        # writes always go to the project's ``.ember/settings.local.json``.
        # The patch below keeps parity with the original test that
        # covered the home-fallback branch — the writer must not
        # accidentally hit the real home directory during CI.
        with patch(
            "ember_code.core.config.tool_permissions.settings_files.Path.home",
            return_value=tmp_path,
        ):
            perms.save_rule("Bash(git push)", "allow")

        settings_path = tmp_path / ".ember" / "settings.local.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert "Bash(git push)" in data["permissions"]["allow"]

    def test_func_to_tool_mapping(self):
        assert FUNC_TO_TOOL["run_shell_command"] == "Bash"
        assert FUNC_TO_TOOL["read_file"] == "Read"
        assert FUNC_TO_TOOL["edit_file"] == "Edit"
        assert FUNC_TO_TOOL["notebook_edit_cell"] == "NotebookEdit"

    def test_check_resolves_func_name(self, tmp_path):
        perms = ToolPermissions(project_dir=tmp_path)
        level = perms.check("", func_name="run_shell_command")
        assert level == "ask"

    def test_unknown_tool_defaults_to_ask(self, tmp_path):
        perms = ToolPermissions(project_dir=tmp_path)
        assert perms.get_level("SomeNewTool") == "ask"
