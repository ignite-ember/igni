"""Tests for utils/context.py — hierarchical rules loading."""

from ember_code.core.utils import context as context_module
from ember_code.core.utils.context import (
    _parse_frontmatter,
    load_project_context,
    load_project_rules,
    load_subdirectory_rules,
    load_user_rules,
)


class TestLoadProjectRules:
    def test_loads_ember_md(self, tmp_path):
        (tmp_path / "ember.md").write_text("ember rules")
        assert load_project_rules(tmp_path) == "ember rules"

    def test_loads_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude rules")
        assert load_project_rules(tmp_path) == "claude rules"

    def test_merges_both_files(self, tmp_path):
        (tmp_path / "ember.md").write_text("ember rules")
        (tmp_path / "CLAUDE.md").write_text("claude rules")
        result = load_project_rules(tmp_path)
        assert "ember rules" in result
        assert "claude rules" in result

    def test_skips_claude_md_when_disabled(self, tmp_path):
        (tmp_path / "ember.md").write_text("ember rules")
        (tmp_path / "CLAUDE.md").write_text("claude rules")
        result = load_project_rules(tmp_path, read_claude_md=False)
        assert "ember rules" in result
        assert "claude rules" not in result

    def test_returns_empty_for_missing(self, tmp_path):
        assert load_project_rules(tmp_path) == ""


class TestLoadSubdirectoryRules:
    def test_collects_subdirectory_rules(self, tmp_path):
        src = tmp_path / "src"
        auth = src / "auth"
        working = auth / "middleware"
        working.mkdir(parents=True)
        (src / "ember.md").write_text("src rules")
        (auth / "ember.md").write_text("auth rules")

        results = load_subdirectory_rules(tmp_path, working)
        assert len(results) == 2
        assert results[0] == ("src", "src rules")
        assert results[1] == ("src/auth", "auth rules")

    def test_collects_claude_md_from_subdirectories(self, tmp_path):
        src = tmp_path / "src"
        working = src / "api"
        working.mkdir(parents=True)
        (src / "CLAUDE.md").write_text("claude src rules")

        results = load_subdirectory_rules(tmp_path, working)
        assert len(results) == 1
        assert results[0] == ("src", "claude src rules")

    def test_merges_both_files_in_subdirectory(self, tmp_path):
        src = tmp_path / "src"
        working = src / "api"
        working.mkdir(parents=True)
        (src / "ember.md").write_text("ember src")
        (src / "CLAUDE.md").write_text("claude src")

        results = load_subdirectory_rules(tmp_path, working)
        assert len(results) == 1
        assert "ember src" in results[0][1]
        assert "claude src" in results[0][1]

    def test_returns_empty_when_no_rules(self, tmp_path):
        working = tmp_path / "src"
        working.mkdir()
        assert load_subdirectory_rules(tmp_path, working) == []

    def test_returns_empty_when_working_dir_is_root(self, tmp_path):
        assert load_subdirectory_rules(tmp_path, tmp_path) == []

    def test_returns_empty_when_working_dir_is_none(self, tmp_path):
        assert load_subdirectory_rules(tmp_path, None) == []

    def test_returns_empty_when_outside_project(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        assert load_subdirectory_rules(tmp_path / "project", other) == []


class TestLoadProjectContext:
    def test_merges_root_rules(self, tmp_path):
        (tmp_path / "ember.md").write_text("root rules")
        result = load_project_context(tmp_path)
        assert "root rules" in result
        assert "Project Rules" in result

    def test_returns_empty_when_no_rules(self, tmp_path):
        assert load_project_context(tmp_path) == ""

    def test_merges_root_and_subdirectory(self, tmp_path):
        (tmp_path / "ember.md").write_text("root rules")
        src = tmp_path / "src"
        src.mkdir()
        (src / "ember.md").write_text("src rules")

        result = load_project_context(tmp_path, working_dir=src)
        assert "root rules" in result
        assert "src rules" in result
        assert "Project Rules" in result
        assert "Directory Rules" in result

    def test_claude_md_at_root(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude project rules")
        result = load_project_context(tmp_path)
        assert "claude project rules" in result

    def test_both_files_at_root(self, tmp_path):
        (tmp_path / "ember.md").write_text("ember root")
        (tmp_path / "CLAUDE.md").write_text("claude root")
        result = load_project_context(tmp_path)
        assert "ember root" in result
        assert "claude root" in result

    def test_skips_claude_md_when_disabled(self, tmp_path):
        (tmp_path / "ember.md").write_text("ember root")
        (tmp_path / "CLAUDE.md").write_text("claude root")
        result = load_project_context(tmp_path, read_claude_md=False)
        assert "ember root" in result
        assert "claude root" not in result

    def test_sections_separated_by_divider(self, tmp_path):
        (tmp_path / "ember.md").write_text("root")
        src = tmp_path / "src"
        src.mkdir()
        (src / "ember.md").write_text("src")

        result = load_project_context(tmp_path, working_dir=src)
        assert "---" in result


class TestParseFrontmatter:
    def test_no_frontmatter(self):
        paths, body = _parse_frontmatter("hello world\n")
        assert paths == []
        assert body == "hello world\n"

    def test_frontmatter_without_paths(self):
        content = "---\nname: test\n---\nbody text\n"
        paths, body = _parse_frontmatter(content)
        assert paths == []
        assert body == "body text\n"

    def test_paths_block(self):
        content = (
            "---\n"
            "paths:\n"
            '  - "**/*.test.ts"\n'
            "  - src/api/**\n"
            "---\n"
            "scoped rule body"
        )
        paths, body = _parse_frontmatter(content)
        assert paths == ["**/*.test.ts", "src/api/**"]
        assert body == "scoped rule body"

    def test_paths_inline_list(self):
        content = '---\npaths: ["a/*", \'b/*\']\n---\nbody'
        paths, body = _parse_frontmatter(content)
        assert paths == ["a/*", "b/*"]
        assert body == "body"


def _redirect_user_rules(monkeypatch, tmp_path, *, with_claude=False):
    """Point the user-rules constants at a sandbox under ``tmp_path``."""
    monkeypatch.setattr(context_module, "USER_RULES_PATH", tmp_path / "rules.md")
    monkeypatch.setattr(context_module, "USER_RULES_DIR", tmp_path / "rules")
    claude_dir = tmp_path / "claude-rules" if with_claude else tmp_path / "nonexistent"
    monkeypatch.setattr(context_module, "CLAUDE_USER_RULES_DIR", claude_dir)


class TestLoadUserRules:
    def test_loads_legacy_single_file(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path)
        (tmp_path / "rules.md").write_text("legacy rules")
        result = load_user_rules()
        assert result == "legacy rules"

    def test_loads_ember_rules_directory(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path)
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "a.md").write_text("rule A")
        (rules_dir / "b.md").write_text("rule B")
        result = load_user_rules()
        assert "rule A" in result
        assert "rule B" in result

    def test_merges_legacy_and_directory(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path)
        (tmp_path / "rules.md").write_text("legacy")
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "one.md").write_text("dir rule")
        result = load_user_rules()
        assert "legacy" in result
        assert "dir rule" in result

    def test_reads_claude_rules_when_enabled(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path, with_claude=True)
        claude_dir = tmp_path / "claude-rules"
        claude_dir.mkdir()
        (claude_dir / "git.md").write_text("git habits")
        result = load_user_rules(read_claude_rules=True)
        assert "git habits" in result

    def test_skips_claude_rules_when_disabled(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path, with_claude=True)
        claude_dir = tmp_path / "claude-rules"
        claude_dir.mkdir()
        (claude_dir / "git.md").write_text("git habits")
        result = load_user_rules(read_claude_rules=False)
        assert "git habits" not in result

    def test_paths_frontmatter_filters_out_when_no_match(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path)
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "tui.md").write_text(
            "---\npaths:\n  - tui/**\n---\ntui-only rule"
        )
        project_dir = tmp_path / "project"
        working_dir = project_dir / "backend"
        working_dir.mkdir(parents=True)
        result = load_user_rules(working_dir=working_dir, project_dir=project_dir)
        assert "tui-only rule" not in result

    def test_paths_frontmatter_includes_when_match(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path)
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "tui.md").write_text(
            "---\npaths:\n  - tui/**\n---\ntui-only rule"
        )
        project_dir = tmp_path / "project"
        working_dir = project_dir / "tui" / "panels"
        working_dir.mkdir(parents=True)
        result = load_user_rules(working_dir=working_dir, project_dir=project_dir)
        assert "tui-only rule" in result

    def test_paths_frontmatter_excluded_without_working_dir(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path)
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "scoped.md").write_text(
            "---\npaths:\n  - any/**\n---\nscoped"
        )
        (rules_dir / "always.md").write_text("always-on rule")
        result = load_user_rules()
        assert "always-on rule" in result
        assert "scoped" not in result

    def test_empty_when_nothing_configured(self, tmp_path, monkeypatch):
        _redirect_user_rules(monkeypatch, tmp_path)
        assert load_user_rules() == ""
