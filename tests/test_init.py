"""Tests for project initialization (init.py)."""

import json
import stat
from pathlib import Path
from unittest.mock import patch

from ember_code.core.init import BUILT_IN_HOOKS, initialize_project

# All tests patch Path.home() so that ~/.ember/ writes go to tmp_path
# instead of the real home directory.


def _patch_home(tmp_path):
    """Return a patch that redirects Path.home() to tmp_path / 'home'."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    return patch.object(Path, "home", return_value=fake_home)


class TestInitializeProject:
    def test_creates_marker_file(self, tmp_path):
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
            assert (tmp_path / "home" / ".ember" / ".initialized").exists()

    def test_returns_true_on_first_run(self, tmp_path):
        with _patch_home(tmp_path):
            assert initialize_project(tmp_path) is True

    def test_returns_false_on_second_run(self, tmp_path):
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
            assert initialize_project(tmp_path) is False

    def test_updates_run_on_second_call(self, tmp_path):
        """Second run still updates built-in files (agents, skills, hooks)."""
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
            # Delete project hooks
            hooks = tmp_path / ".ember" / "hooks"
            if hooks.exists():
                import shutil

                shutil.rmtree(hooks)

            # Second run should recreate hooks (update always runs)
            initialize_project(tmp_path)
            assert (tmp_path / ".ember" / "hooks").exists()

    def test_creates_ember_directory(self, tmp_path):
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
            assert (tmp_path / ".ember").is_dir()

    def test_existing_ember_dir_not_destroyed(self, tmp_path):
        ember_dir = tmp_path / ".ember"
        ember_dir.mkdir()
        (ember_dir / "custom.txt").write_text("keep me")

        with _patch_home(tmp_path):
            initialize_project(tmp_path)
            assert (ember_dir / "custom.txt").read_text() == "keep me"


class TestHomeModelMigration:
    """Older versions wrote a bundled Ember Cloud model entry into
    every home config. Hosted models now come from cloud discovery
    on session start, so those bundled rows are stale and get
    stripped on upgrade. User-customised entries are left alone."""

    def _write_home_config(self, tmp_path: Path, body: str) -> Path:
        home_ember = tmp_path / "home" / ".ember"
        home_ember.mkdir(parents=True, exist_ok=True)
        # ``.initialized`` marker simulates a prior install so the
        # migration branch runs (not the first-run bootstrap).
        (home_ember / ".initialized").touch()
        (home_ember / "config.yaml").write_text(body)
        return home_ember / "config.yaml"

    def test_legacy_m25_bundle_is_removed(self, tmp_path):
        """Old M2.5 bundled cloud entry is fully stripped. The
        ``default`` field, which named the now-deleted entry, is
        cleared so cloud discovery (or the resolver's first-in-
        registry fallback) picks something current."""
        import yaml

        path = self._write_home_config(
            tmp_path,
            "models:\n"
            "  default: MiniMax-M2.5\n"
            "  registry:\n"
            "    MiniMax-M2.5:\n"
            "      provider: openai_like\n"
            "      model_id: MiniMaxAI/MiniMax-M2.5\n"
            "      url: https://api.ignite-ember.sh/v1\n"
            "      api_key: cloud_token\n"
            "      context_window: 204800\n"
            "      vision: false\n",
        )
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        data = yaml.safe_load(path.read_text()) or {}
        # The whole models block tidied away — no leftover empty
        # registry or empty default.
        assert "models" not in data

    def test_legacy_m27_bundle_is_also_removed(self, tmp_path):
        """The M2.7 bundled entry from the intermediate release is
        recognised the same way (Ember Cloud URL + ``cloud_token``)
        and stripped — we never want a shadowed copy of a
        cloud-discovered entry sitting in home config."""
        import yaml

        path = self._write_home_config(
            tmp_path,
            "models:\n"
            "  default: MiniMax-M2.7\n"
            "  registry:\n"
            "    MiniMax-M2.7:\n"
            "      provider: openai_like\n"
            "      model_id: MiniMaxAI/MiniMax-M2.7\n"
            "      url: https://api.ignite-ember.sh/v1\n"
            "      api_key: cloud_token\n"
            "      context_window: 204800\n"
            "      vision: false\n",
        )
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        data = yaml.safe_load(path.read_text()) or {}
        assert "models" not in data

    def test_customised_entry_is_kept(self, tmp_path):
        """User pointed an entry at their own endpoint — migration
        must not touch it. The customised URL (not
        ``ignite-ember.sh``) is the signal."""
        path = self._write_home_config(
            tmp_path,
            "models:\n"
            "  default: my-corp-model\n"
            "  registry:\n"
            "    my-corp-model:\n"
            "      provider: openai_like\n"
            "      model_id: my-corp/model-v1\n"
            "      url: https://my-corp-proxy.internal/v1\n"
            "      api_key: my_custom_token\n",
        )
        before = path.read_text()
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        assert path.read_text() == before

    def test_mixed_config_keeps_only_custom(self, tmp_path):
        """Home config with both a bundled cloud entry and a custom
        one: bundled is dropped, custom is kept, ``default`` stays
        because it pointed at the custom entry."""
        import yaml

        path = self._write_home_config(
            tmp_path,
            "models:\n"
            "  default: my-model\n"
            "  registry:\n"
            "    MiniMax-M2.5:\n"
            "      provider: openai_like\n"
            "      model_id: MiniMaxAI/MiniMax-M2.5\n"
            "      url: https://api.ignite-ember.sh/v1\n"
            "      api_key: cloud_token\n"
            "    my-model:\n"
            "      provider: openai_like\n"
            "      model_id: my/model\n"
            "      url: https://my.example/v1\n"
            "      api_key: my_token\n",
        )
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        data = yaml.safe_load(path.read_text()) or {}
        assert "MiniMax-M2.5" not in data["models"]["registry"]
        assert "my-model" in data["models"]["registry"]
        assert data["models"]["default"] == "my-model"

    def test_no_op_when_no_bundled_entries(self, tmp_path):
        """Config without any bundled cloud entries is byte-stable
        across restarts — migration is idempotent."""
        path = self._write_home_config(
            tmp_path,
            "models:\n  default: gpt-4o\n  registry:\n    gpt-4o:\n      provider: openai_like\n",
        )
        before = path.read_text()
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        assert path.read_text() == before


class TestAgentCopy:
    def test_copies_builtin_agents(self, tmp_path):
        import ember_code.core.init as init_mod

        original = init_mod.PACKAGE_DIR

        fake_root = tmp_path / "fake_root"
        fake_root.mkdir()
        agents_dir = fake_root / "bundled_agents"
        agents_dir.mkdir()
        (agents_dir / "editor.md").write_text("editor content")
        (agents_dir / "docs.md").write_text("docs content")

        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()
            with _patch_home(tmp_path):
                initialize_project(project)

            copied = project / ".ember" / "agents"
            assert (copied / "editor.md").exists()
            assert (copied / "docs.md").exists()
            assert (copied / "editor.md").read_text() == "editor content"
        finally:
            init_mod.PACKAGE_DIR = original

    def test_does_not_overwrite_existing_agents(self, tmp_path):
        import ember_code.core.init as init_mod

        original = init_mod.PACKAGE_DIR

        fake_root = tmp_path / "fake_root"
        (fake_root / "bundled_agents").mkdir(parents=True)
        (fake_root / "bundled_agents" / "editor.md").write_text("builtin version")
        (fake_root / "bundled_skills").mkdir()

        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()
            agents_dir = project / ".ember" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "editor.md").write_text("user version")

            with _patch_home(tmp_path):
                initialize_project(project)
            assert (agents_dir / "editor.md").read_text() == "user version"
        finally:
            init_mod.PACKAGE_DIR = original


class TestSkillCopy:
    def test_copies_builtin_skills(self, tmp_path):
        import ember_code.core.init as init_mod

        original = init_mod.PACKAGE_DIR

        fake_root = tmp_path / "fake_root"
        (fake_root / "bundled_agents").mkdir(parents=True)
        skills_dir = fake_root / "bundled_skills"
        (skills_dir / "commit").mkdir(parents=True)
        (skills_dir / "commit" / "SKILL.md").write_text("commit skill")
        (skills_dir / "simplify").mkdir(parents=True)
        (skills_dir / "simplify" / "SKILL.md").write_text("simplify skill")

        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()
            with _patch_home(tmp_path):
                initialize_project(project)

            copied = project / ".ember" / "skills"
            assert (copied / "commit" / "SKILL.md").exists()
            assert (copied / "simplify" / "SKILL.md").exists()
            assert (copied / "commit" / "SKILL.md").read_text() == "commit skill"
        finally:
            init_mod.PACKAGE_DIR = original

    def test_ignores_non_skill_directories(self, tmp_path):
        import ember_code.core.init as init_mod

        original = init_mod.PACKAGE_DIR

        fake_root = tmp_path / "fake_root"
        (fake_root / "bundled_agents").mkdir(parents=True)
        skills_dir = fake_root / "bundled_skills"
        (skills_dir / "broken").mkdir(parents=True)
        (skills_dir / "broken" / "README.md").write_text("not a skill")

        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()
            with _patch_home(tmp_path):
                initialize_project(project)
            assert not (project / ".ember" / "skills" / "broken").exists()
        finally:
            init_mod.PACKAGE_DIR = original


class TestHookProvisioning:
    def test_writes_hook_scripts(self, tmp_path):
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        hooks_dir = tmp_path / ".ember" / "hooks"
        for hook in BUILT_IN_HOOKS:
            script = hooks_dir / hook["filename"]
            assert script.exists()
            assert script.stat().st_mode & stat.S_IXUSR  # executable

    def test_registers_hooks_in_settings(self, tmp_path):
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        # Settings written to project .ember/settings.json
        settings = json.loads((tmp_path / ".ember" / "settings.json").read_text())
        assert "hooks" in settings
        assert "PreToolUse" in settings["hooks"]
        assert any(
            h["command"] == ".ember/hooks/pre-pr-review.sh" for h in settings["hooks"]["PreToolUse"]
        )

    def test_preserves_existing_settings(self, tmp_path):
        project_ember = tmp_path / ".ember"
        project_ember.mkdir(parents=True)
        (project_ember / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["Read"]}})
        )

        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        settings = json.loads((project_ember / "settings.json").read_text())
        assert settings["permissions"]["allow"] == ["Read"]
        assert "hooks" in settings


class TestEmberMd:
    def test_creates_ember_md(self, tmp_path):
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        path = tmp_path / "ember.md"
        assert path.exists()
        assert "Project Context" in path.read_text()

    def test_does_not_overwrite_existing_ember_md(self, tmp_path):
        (tmp_path / "ember.md").write_text("my custom context")
        with _patch_home(tmp_path):
            initialize_project(tmp_path)
        assert (tmp_path / "ember.md").read_text() == "my custom context"


class TestChecksumUpdate:
    """Tests for checksum-based update of built-in agents/skills."""

    def _setup_fake_root(self, tmp_path, agent_content="v1 content"):
        import ember_code.core.init as init_mod

        fake_root = tmp_path / "fake_root"
        (fake_root / "bundled_agents").mkdir(parents=True)
        (fake_root / "bundled_agents" / "editor.md").write_text(agent_content)
        (fake_root / "bundled_skills").mkdir()
        return fake_root, init_mod

    def test_untouched_file_gets_updated(self, tmp_path):
        """Package updated + user didn't modify → overwrite."""
        fake_root, init_mod = self._setup_fake_root(tmp_path, "v1 content")
        original = init_mod.PACKAGE_DIR
        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()

            # First init — copies v1
            with _patch_home(tmp_path):
                initialize_project(project)
            assert (project / ".ember" / "agents" / "editor.md").read_text() == "v1 content"

            # Simulate package update — change the source file
            (fake_root / "bundled_agents" / "editor.md").write_text("v2 content")

            # Second run — should overwrite since user didn't modify
            with _patch_home(tmp_path):
                initialize_project(project)
            assert (project / ".ember" / "agents" / "editor.md").read_text() == "v2 content"
        finally:
            init_mod.PACKAGE_DIR = original

    def test_user_modified_file_kept_with_new(self, tmp_path):
        """Package updated + user modified → keep user version, write .new file."""
        fake_root, init_mod = self._setup_fake_root(tmp_path, "v1 content")
        original = init_mod.PACKAGE_DIR
        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()

            # First init
            with _patch_home(tmp_path):
                initialize_project(project)

            # User modifies the file
            (project / ".ember" / "agents" / "editor.md").write_text("my custom agent")

            # Package updates
            (fake_root / "bundled_agents" / "editor.md").write_text("v2 content")

            # Second run — should keep user version and write .new
            with _patch_home(tmp_path):
                initialize_project(project)
            assert (project / ".ember" / "agents" / "editor.md").read_text() == "my custom agent"
            assert (project / ".ember" / "agents" / "editor.md.new").read_text() == "v2 content"
        finally:
            init_mod.PACKAGE_DIR = original

    def test_new_package_file_copied(self, tmp_path):
        """New file in package → copied to project."""
        fake_root, init_mod = self._setup_fake_root(tmp_path, "editor v1")
        original = init_mod.PACKAGE_DIR
        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()

            # First init
            with _patch_home(tmp_path):
                initialize_project(project)

            # Add new agent to package
            (fake_root / "bundled_agents" / "new-agent.md").write_text("new agent content")

            # Second run — should copy the new file
            with _patch_home(tmp_path):
                initialize_project(project)
            assert (
                project / ".ember" / "agents" / "new-agent.md"
            ).read_text() == "new agent content"
        finally:
            init_mod.PACKAGE_DIR = original

    def test_user_custom_files_not_deleted(self, tmp_path):
        """User's custom agents not in package → never touched."""
        fake_root, init_mod = self._setup_fake_root(tmp_path, "editor v1")
        original = init_mod.PACKAGE_DIR
        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()

            with _patch_home(tmp_path):
                initialize_project(project)

            # User creates their own custom agent
            (project / ".ember" / "agents" / "my-custom.md").write_text("custom agent")

            # Second run — custom file should survive
            with _patch_home(tmp_path):
                initialize_project(project)
            assert (project / ".ember" / "agents" / "my-custom.md").read_text() == "custom agent"
        finally:
            init_mod.PACKAGE_DIR = original

    def test_checksums_file_created(self, tmp_path):
        """Checksums file is created after init."""
        fake_root, init_mod = self._setup_fake_root(tmp_path, "content")
        original = init_mod.PACKAGE_DIR
        init_mod.PACKAGE_DIR = fake_root
        try:
            project = tmp_path / "project"
            project.mkdir()
            with _patch_home(tmp_path):
                initialize_project(project)
            assert (project / ".ember" / ".checksums.json").exists()
        finally:
            init_mod.PACKAGE_DIR = original
