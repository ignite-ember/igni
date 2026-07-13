"""Tests for config/tool_permissions.py — permission resolution with argument rules."""

import json
from unittest.mock import patch

from ember_code.core.config.tool_permissions import (
    FUNC_TO_TOOL,
    ToolPermissions,
    _args_to_str,
    _extract_domain,
    _match_rule_args,
    _parse_rule,
)


class TestParseRule:
    def test_bare_tool(self):
        assert _parse_rule("Bash") == ("Bash", None)

    def test_tool_with_args(self):
        assert _parse_rule("Bash(git status)") == ("Bash", "git status")

    def test_tool_with_pattern(self):
        assert _parse_rule("WebFetch(domain:github.com)") == ("WebFetch", "domain:github.com")

    def test_whitespace(self):
        assert _parse_rule("  Read  ") == ("Read", None)


class TestArgsToStr:
    def test_none(self):
        assert _args_to_str(None) == ""

    def test_args_list(self):
        assert _args_to_str({"args": ["git", "push"]}) == "git push"

    def test_path_key(self):
        assert _args_to_str({"path": "/src/main.py"}) == "/src/main.py"

    def test_file_path_key(self):
        assert _args_to_str({"file_path": "test.py"}) == "test.py"

    def test_fallback(self):
        result = _args_to_str({"x": "hello", "y": "world"})
        assert "hello" in result
        assert "world" in result


class TestExtractDomain:
    def test_simple_url(self):
        assert _extract_domain("https://github.com/foo") == "github.com"

    def test_with_port(self):
        assert _extract_domain("http://localhost:3000/api") == "localhost:3000"

    def test_invalid(self):
        assert _extract_domain("not a url") == ""


class TestMatchRuleArgs:
    def test_exact_match(self):
        assert _match_rule_args("git status", "Bash", {"args": ["git", "status"]})

    def test_prefix_wildcard(self):
        assert _match_rule_args("git:*", "Bash", {"args": ["git", "push", "origin"]})

    def test_domain_match(self):
        assert _match_rule_args("domain:github.com", "WebFetch", {"url": "https://github.com/foo"})

    def test_domain_mismatch(self):
        assert not _match_rule_args("domain:github.com", "WebFetch", {"url": "https://evil.com"})

    def test_path_match(self):
        assert _match_rule_args("path:src/*", "Read", {"file_path": "src/main.py"})

    def test_path_mismatch(self):
        assert not _match_rule_args("path:src/*", "Read", {"file_path": "tests/test.py"})


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
        with patch("ember_code.core.config.tool_permissions.Path.home", return_value=tmp_path):
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
