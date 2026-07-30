"""Tests for skills — parser, loader, pool."""

from pathlib import Path

from ember_code.core.skills.loader import SkillPool, SkillPriority
from ember_code.core.skills.parser import SkillDefinition, SkillParser


class TestSkillParser:
    def test_parse_valid_skill(self, sample_skill_md):
        defn = SkillParser.parse(sample_skill_md)
        assert defn.name == "test-skill"
        assert defn.description == "A test skill"
        assert defn.argument_hint == "<arg>"
        assert "$ARGUMENTS" in defn.body

    def test_parse_no_frontmatter_uses_dirname(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("Just a body with no frontmatter.\n")

        defn = SkillParser.parse(skill_file)
        assert defn.name == "my-skill"
        assert "Just a body" in defn.body

    def test_parse_defaults(self, tmp_path):
        skill_dir = tmp_path / "minimal"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("---\nname: minimal\n---\nBody\n")

        defn = SkillParser.parse(skill_file)
        assert defn.name == "minimal"
        assert defn.description == ""
        assert defn.version == "0.1.0"
        assert defn.user_invocable is True
        assert defn.context == "inline"

    def test_parse_name_defaults_to_dirname(self, tmp_path):
        skill_dir = tmp_path / "deploy"
        skill_dir.mkdir()
        f = skill_dir / "SKILL.md"
        f.write_text("---\ndescription: Deploy things\n---\nBody\n")

        defn = SkillParser.parse(f)
        assert defn.name == "deploy"


class TestSkillDefinition:
    def test_render_arguments(self):
        defn = SkillDefinition(
            name="test",
            body="Review $ARGUMENTS carefully.",
        )
        result = defn.render("my_file.py")
        assert result == "Review my_file.py carefully."

    def test_render_positional_args(self):
        defn = SkillDefinition(name="test", body="First: $1, Second: $2")
        result = defn.render("alpha beta")
        assert result == "First: alpha, Second: beta"

    def test_render_skill_dir(self, tmp_path):
        defn = SkillDefinition(
            name="test",
            body="Dir: ${EMBER_SKILL_DIR}/template.txt",
            source_dir=tmp_path / "skills" / "test",
        )
        result = defn.render()
        assert str(tmp_path / "skills" / "test") in result

    def test_render_claude_skill_dir_compat(self, tmp_path):
        defn = SkillDefinition(
            name="test",
            body="Dir: ${CLAUDE_SKILL_DIR}/template.txt",
            source_dir=tmp_path,
        )
        result = defn.render()
        assert str(tmp_path) in result

    def test_render_empty_arguments(self):
        defn = SkillDefinition(name="test", body="Do $ARGUMENTS now.")
        result = defn.render("")
        assert result == "Do  now."

    def test_render_session_id(self):
        defn = SkillDefinition(name="test", body="Session: ${EMBER_SESSION_ID}")
        result = defn.render("", session_id="abc-123")
        assert result == "Session: abc-123"

    def test_render_session_id_empty_default(self):
        defn = SkillDefinition(name="test", body="Session: ${EMBER_SESSION_ID}")
        result = defn.render("")
        assert result == "Session: "


class TestSkillPool:
    def test_empty_pool(self):
        pool = SkillPool()
        assert pool.list_skills() == []

    def test_load_directory(self, tmp_path):
        for name in ["alpha", "beta"]:
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Skill {name}\n---\nBody\n"
            )

        pool = SkillPool()
        pool.load_directory(tmp_path, priority=0)
        names = [s.name for s in pool.list_skills()]
        assert sorted(names) == ["alpha", "beta"]

    def test_priority_override(self, tmp_path):
        low = tmp_path / "low"
        low.mkdir()
        d = low / "shared"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: shared\ndescription: Low\n---\n")

        high = tmp_path / "high"
        high.mkdir()
        d = high / "shared"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: shared\ndescription: High\n---\n")

        pool = SkillPool()
        pool.load_directory(low, priority=0)
        pool.load_directory(high, priority=3)
        assert pool.get("shared").description == "High"

    def test_get_unknown_returns_none(self):
        pool = SkillPool()
        assert pool.get("nonexistent") is None

    def test_match_user_command(self, tmp_path):
        d = tmp_path / "commit"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: commit\ndescription: Git commit\n---\nBody\n")

        pool = SkillPool()
        pool.load_directory(tmp_path, priority=0)

        result = pool.match_user_command("/commit fix typo")
        assert result is not None
        skill, args = result
        assert skill.name == "commit"
        assert args == "fix typo"

    def test_match_no_slash(self):
        pool = SkillPool()
        assert pool.match_user_command("not a command") is None

    def test_match_unknown_skill(self):
        pool = SkillPool()
        assert pool.match_user_command("/nonexistent") is None

    def test_describe(self, tmp_path):
        d = tmp_path / "explain"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: explain\ndescription: Explain code\nargument-hint: <file>\n---\n"
        )

        pool = SkillPool()
        pool.load_directory(tmp_path, priority=0)
        desc = pool.describe()
        assert "/explain" in desc
        assert "Explain code" in desc

    def test_load_skips_non_dirs(self, tmp_path):
        (tmp_path / "not_a_dir.txt").write_text("nope")
        pool = SkillPool()
        pool.load_directory(tmp_path, priority=0)
        assert pool.list_skills() == []

    def test_load_skips_dirs_without_skill_md(self, tmp_path):
        (tmp_path / "empty_skill").mkdir()
        pool = SkillPool()
        pool.load_directory(tmp_path, priority=0)
        assert pool.list_skills() == []


class TestSkillResolutionOrder:
    """Pin down the documented resolution order so a future reorder of
    ``load_all`` can't silently flip who wins on a name collision.

    See the module docstring of ``skills/loader.py`` for the canonical
    table. These tests assert the strict order: project Ember > project
    local > project Claude > user Ember > user Claude > bundled.
    """

    def _write_skill(self, root: Path, name: str, label: str) -> None:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {label}\n---\nbody\n")

    def _build_layout(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a fake ``$HOME`` and project under ``tmp_path``."""
        home = tmp_path / "home"
        project = tmp_path / "project"
        home.mkdir()
        project.mkdir()
        return home, project

    def _load(self, monkeypatch, home: Path, project: Path, cross_tool: bool = True):
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        pool = SkillPool()
        pool.load_all(project_dir=project, cross_tool_support=cross_tool)
        return pool

    def test_user_ember_beats_user_claude_at_same_scope(self, tmp_path, monkeypatch):
        home, project = self._build_layout(tmp_path)
        self._write_skill(home / ".ember" / "skills", "shared", "ember-user")
        self._write_skill(home / ".claude" / "skills", "shared", "claude-user")
        pool = self._load(monkeypatch, home, project)
        assert pool.get("shared").description == "ember-user"

    def test_project_ember_beats_project_claude_at_same_scope(self, tmp_path, monkeypatch):
        home, project = self._build_layout(tmp_path)
        self._write_skill(project / ".ember" / "skills", "shared", "ember-project")
        self._write_skill(project / ".claude" / "skills", "shared", "claude-project")
        pool = self._load(monkeypatch, home, project)
        assert pool.get("shared").description == "ember-project"

    def test_project_beats_user_across_scopes(self, tmp_path, monkeypatch):
        home, project = self._build_layout(tmp_path)
        self._write_skill(home / ".ember" / "skills", "shared", "ember-user")
        self._write_skill(project / ".ember" / "skills", "shared", "ember-project")
        pool = self._load(monkeypatch, home, project)
        assert pool.get("shared").description == "ember-project"

    def test_project_claude_beats_user_ember(self, tmp_path, monkeypatch):
        """Cross-scope: a project's Claude config overrides the user's
        global Ember preferences. This is the deliberate semantic of
        the explicit priority scheme."""
        home, project = self._build_layout(tmp_path)
        self._write_skill(home / ".ember" / "skills", "shared", "ember-user")
        self._write_skill(project / ".claude" / "skills", "shared", "claude-project")
        pool = self._load(monkeypatch, home, project)
        assert pool.get("shared").description == "claude-project"

    def test_user_claude_beats_bundled(self, tmp_path, monkeypatch):
        """User-level Claude skills should override silent bundled defaults."""
        home, project = self._build_layout(tmp_path)
        # Stub a bundled skill by writing into the actual bundled_skills dir
        # would be invasive; instead verify the priority constants directly.
        assert SkillPriority.USER_CLAUDE > SkillPriority.BUNDLED

    def test_local_beats_project_claude(self, tmp_path, monkeypatch):
        home, project = self._build_layout(tmp_path)
        self._write_skill(project / ".ember" / "skills.local", "shared", "ember-local")
        self._write_skill(project / ".claude" / "skills", "shared", "claude-project")
        pool = self._load(monkeypatch, home, project)
        assert pool.get("shared").description == "ember-local"

    def test_full_chain_yields_project_ember(self, tmp_path, monkeypatch):
        """All six sources define ``shared``; the project-Ember one wins."""
        home, project = self._build_layout(tmp_path)
        self._write_skill(home / ".claude" / "skills", "shared", "claude-user")
        self._write_skill(home / ".ember" / "skills", "shared", "ember-user")
        self._write_skill(project / ".claude" / "skills", "shared", "claude-project")
        self._write_skill(project / ".ember" / "skills.local", "shared", "ember-local")
        self._write_skill(project / ".ember" / "skills", "shared", "ember-project")
        pool = self._load(monkeypatch, home, project)
        assert pool.get("shared").description == "ember-project"

    def test_claude_skipped_when_cross_tool_disabled(self, tmp_path, monkeypatch):
        home, project = self._build_layout(tmp_path)
        self._write_skill(project / ".claude" / "skills", "claude-only", "claude")
        pool = self._load(monkeypatch, home, project, cross_tool=False)
        assert pool.get("claude-only") is None
