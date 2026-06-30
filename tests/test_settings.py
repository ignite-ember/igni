"""Tests for config/settings.py."""

import pytest

from ember_code.core.config.settings import (
    ModelsConfig,
    PermissionsConfig,
    Settings,
    _deep_merge,
    _load_yaml,
    load_settings,
)
from ember_code.core.config.settings import (
    _platform_managed_settings_path as _REAL_PLATFORM_PATH,
)


@pytest.fixture(autouse=True)
def _isolate_managed_settings(monkeypatch):
    """Default every settings test to "no managed policy deployed."

    Tests that care about the managed tier explicitly override this
    via their own monkeypatch — the autouse just makes sure stray
    OS-level files don't bleed into hermetic tests. The real platform
    function is captured at module load time as
    ``_REAL_PLATFORM_PATH`` so platform-detail tests can still reach
    it past this stub."""
    monkeypatch.setattr(
        "ember_code.core.config.settings._platform_managed_settings_path",
        lambda: None,
    )


class TestDeepMerge:
    def test_flat_merge(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"models": {"default": "a", "registry": {}}}
        override = {"models": {"default": "c"}}
        result = _deep_merge(base, override)
        assert result == {"models": {"default": "c", "registry": {}}}

    def test_deep_nested_merge(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"c": 99}}}
        result = _deep_merge(base, override)
        assert result["a"]["b"]["c"] == 99
        assert result["a"]["b"]["d"] == 2

    def test_override_replaces_non_dict(self):
        base = {"a": {"b": 1}}
        override = {"a": "replaced"}
        result = _deep_merge(base, override)
        assert result["a"] == "replaced"

    def test_does_not_mutate_base(self):
        base = {"a": 1}
        override = {"a": 2}
        _deep_merge(base, override)
        assert base["a"] == 1


class TestLoadYaml:
    def test_loads_valid_yaml(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("models:\n  default: test-model\n")
        result = _load_yaml(f)
        assert result == {"models": {"default": "test-model"}}

    def test_returns_empty_for_missing_file(self, tmp_path):
        result = _load_yaml(tmp_path / "missing.yaml")
        assert result == {}

    def test_returns_empty_for_non_dict_yaml(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("just a string\n")
        result = _load_yaml(f)
        assert result == {}

    def test_returns_empty_for_null_yaml(self, tmp_path):
        f = tmp_path / "null.yaml"
        f.write_text("---\n")
        result = _load_yaml(f)
        assert result == {}


class TestSettings:
    def test_default_settings(self):
        s = Settings()
        # Hosted models are populated by cloud discovery on session
        # start, not shipped in the package defaults — so a freshly-
        # constructed ``Settings()`` carries an empty default and an
        # empty registry. The resolver falls back to first-in-registry
        # at lookup time once cloud discovery has merged its entries.
        assert s.models.default == ""
        assert s.models.registry == {}
        assert s.permissions.file_read == "allow"
        assert s.permissions.file_write == "ask"
        assert s.orchestration.max_nesting_depth == 5
        assert s.display.show_routing is False

    def test_custom_model(self):
        s = Settings(models=ModelsConfig(default="custom-model"))
        assert s.models.default == "custom-model"

    def test_permissions_override(self):
        s = Settings(permissions=PermissionsConfig(file_write="allow", shell_execute="deny"))
        assert s.permissions.file_write == "allow"
        assert s.permissions.shell_execute == "deny"
        assert s.permissions.file_read == "allow"

    def test_protected_paths_default(self):
        s = Settings()
        assert ".env" in s.safety.protected_paths
        assert ".env.*" in s.safety.protected_paths
        assert "*.pem" in s.safety.protected_paths
        assert "*.key" in s.safety.protected_paths
        assert "credentials.*" in s.safety.protected_paths


class TestLoadSettings:
    def test_load_with_project_config(self, tmp_path):
        ember_dir = tmp_path / ".ember"
        ember_dir.mkdir()
        config = ember_dir / "config.yaml"
        config.write_text("models:\n  default: custom-from-project\n")

        s = load_settings(project_dir=tmp_path)
        assert s.models.default == "custom-from-project"

    def test_load_with_cli_overrides(self, tmp_path):
        s = load_settings(
            cli_overrides={"models": {"default": "cli-model"}},
            project_dir=tmp_path,
        )
        assert s.models.default == "cli-model"

    def test_cli_overrides_beat_project(self, tmp_path):
        ember_dir = tmp_path / ".ember"
        ember_dir.mkdir()
        config = ember_dir / "config.yaml"
        config.write_text("models:\n  default: project-model\n")

        s = load_settings(
            cli_overrides={"models": {"default": "cli-model"}},
            project_dir=tmp_path,
        )
        assert s.models.default == "cli-model"

    def test_local_config_beats_project(self, tmp_path):
        ember_dir = tmp_path / ".ember"
        ember_dir.mkdir()
        (ember_dir / "config.yaml").write_text("models:\n  default: project\n")
        (ember_dir / "config.local.yaml").write_text("models:\n  default: local\n")

        s = load_settings(project_dir=tmp_path)
        assert s.models.default == "local"

    def test_user_global_config_loaded(self, tmp_path):
        # The user tier is the lowest-priority FILE source — sits
        # under built-in defaults, gets overridden by everything
        # above it. Hit it in isolation: only ``~/.ember/config.yaml``
        # exists, no project files, no CLI.
        # ``conftest._isolate_user_settings`` already redirects
        # ``Path.home()`` to a per-test tmp dir, so writing
        # ``Path.home() / .ember / config.yaml`` writes inside the
        # fake home without touching the developer's real config.
        from pathlib import Path

        user_ember = Path.home() / ".ember"
        user_ember.mkdir(parents=True)
        (user_ember / "config.yaml").write_text("models:\n  default: user-global-model\n")
        s = load_settings(project_dir=tmp_path)
        assert s.models.default == "user-global-model"

    def test_project_beats_user_global(self, tmp_path):
        from pathlib import Path

        user_ember = Path.home() / ".ember"
        user_ember.mkdir(parents=True)
        (user_ember / "config.yaml").write_text("models:\n  default: user-global\n")
        project_ember = tmp_path / ".ember"
        project_ember.mkdir()
        (project_ember / "config.yaml").write_text("models:\n  default: project-model\n")
        s = load_settings(project_dir=tmp_path)
        # Project wins over user — same precedence as YAML files in
        # the CC tier model.
        assert s.models.default == "project-model"


class TestFiveTierPrecedence:
    """Pins the FULL precedence stack in one go: managed > CLI >
    project.local > project > user > built-in defaults. Each
    tier writes the same key with a different value; the assertion
    walks DOWN by removing the winning tier and re-running, so a
    silent reordering of the precedence (e.g. a refactor that
    flips two ``_deep_merge`` calls) gets caught in this test
    rather than at the customer site."""

    def _write_all_tiers(self, tmp_path, monkeypatch, *, value: str, tier_label: str) -> None:
        """Helper: stage every tier with values keyed to a label
        so each test can verify which one wins for that value.
        Returns once all tiers are present."""
        from pathlib import Path

        # Tier 5: user global
        user_ember = Path.home() / ".ember"
        user_ember.mkdir(parents=True, exist_ok=True)
        (user_ember / "config.yaml").write_text(f"models:\n  default: user-{tier_label}\n")
        # Tiers 4 + 3: project + project.local
        project_ember = tmp_path / ".ember"
        project_ember.mkdir(parents=True, exist_ok=True)
        (project_ember / "config.yaml").write_text(f"models:\n  default: project-{tier_label}\n")
        (project_ember / "config.local.yaml").write_text(
            f"models:\n  default: local-{tier_label}\n"
        )
        # Tier 1: managed policy (sits at the top)
        managed = tmp_path / "managed.yaml"
        managed.write_text(f"models:\n  default: managed-{tier_label}\n")
        monkeypatch.setattr(
            "ember_code.core.config.settings._platform_managed_settings_path",
            lambda: managed,
        )

    def test_managed_is_top_of_stack(self, tmp_path, monkeypatch):
        # All five tiers present; managed must win.
        self._write_all_tiers(tmp_path, monkeypatch, value="x", tier_label="x")
        s = load_settings(
            cli_overrides={"models": {"default": "cli-x"}},
            project_dir=tmp_path,
        )
        assert s.models.default == "managed-x"

    def test_cli_wins_when_no_managed(self, tmp_path, monkeypatch):
        # Drop managed → CLI takes over the top.
        self._write_all_tiers(tmp_path, monkeypatch, value="x", tier_label="x")
        # Clear the managed file but keep the lookup path intact.
        (tmp_path / "managed.yaml").unlink()
        s = load_settings(
            cli_overrides={"models": {"default": "cli-x"}},
            project_dir=tmp_path,
        )
        assert s.models.default == "cli-x"

    def test_project_local_wins_when_no_cli_no_managed(self, tmp_path, monkeypatch) -> None:
        self._write_all_tiers(tmp_path, monkeypatch, value="x", tier_label="x")
        (tmp_path / "managed.yaml").unlink()
        s = load_settings(project_dir=tmp_path)
        assert s.models.default == "local-x"

    def test_project_wins_when_only_user_and_project(self, tmp_path) -> None:
        from pathlib import Path

        user_ember = Path.home() / ".ember"
        user_ember.mkdir(parents=True, exist_ok=True)
        (user_ember / "config.yaml").write_text("models:\n  default: user-only\n")
        project_ember = tmp_path / ".ember"
        project_ember.mkdir(parents=True, exist_ok=True)
        (project_ember / "config.yaml").write_text("models:\n  default: project-only\n")
        s = load_settings(project_dir=tmp_path)
        assert s.models.default == "project-only"


class TestPlatformManagedSettingsPath:
    """The autouse fixture neutralises the live module attribute;
    these tests reach the real function via ``_REAL_PLATFORM_PATH``
    captured at module import time."""

    def test_darwin_path(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        p = _REAL_PLATFORM_PATH()
        assert p is not None
        assert str(p) == "/Library/Application Support/Ember/managed-settings.yaml"

    def test_linux_path(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        p = _REAL_PLATFORM_PATH()
        assert p is not None
        assert str(p) == "/etc/ember/managed-settings.yaml"

    def test_win32_uses_programdata(self, monkeypatch):
        monkeypatch.setenv("PROGRAMDATA", r"C:\TestProgramData")
        monkeypatch.setattr("sys.platform", "win32")
        p = _REAL_PLATFORM_PATH()
        assert p is not None
        assert "Ember" in str(p)
        assert "managed-settings.yaml" in str(p)

    def test_unknown_platform_returns_none(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "freebsd")
        assert _REAL_PLATFORM_PATH() is None


class TestManagedSettings:
    """The managed-policy tier is the 5th precedence level — it wins
    over CLI flags. Used by sysadmins/MDM to enforce org policy that
    a user can't override locally."""

    def test_managed_overrides_user_global(self, tmp_path, monkeypatch):
        managed = tmp_path / "managed.yaml"
        managed.write_text("models:\n  default: org-pinned\n")
        monkeypatch.setattr(
            "ember_code.core.config.settings._platform_managed_settings_path",
            lambda: managed,
        )
        s = load_settings(project_dir=tmp_path)
        assert s.models.default == "org-pinned"

    def test_managed_overrides_project(self, tmp_path, monkeypatch):
        ember_dir = tmp_path / ".ember"
        ember_dir.mkdir()
        (ember_dir / "config.yaml").write_text("models:\n  default: project-model\n")
        managed = tmp_path / "managed.yaml"
        managed.write_text("models:\n  default: org-pinned\n")
        monkeypatch.setattr(
            "ember_code.core.config.settings._platform_managed_settings_path",
            lambda: managed,
        )
        s = load_settings(project_dir=tmp_path)
        assert s.models.default == "org-pinned"

    def test_managed_overrides_cli(self, tmp_path, monkeypatch):
        """Headline invariant: managed beats CLI. A user can't
        ``--auto-approve`` their way out of an org policy."""
        managed = tmp_path / "managed.yaml"
        managed.write_text("permissions:\n  mode: dontAsk\nmodels:\n  default: org-pinned\n")
        monkeypatch.setattr(
            "ember_code.core.config.settings._platform_managed_settings_path",
            lambda: managed,
        )
        s = load_settings(
            cli_overrides={
                "models": {"default": "user-cli-choice"},
                "permissions": {"mode": "bypassPermissions"},
            },
            project_dir=tmp_path,
        )
        # CLI lost both fights — managed wins.
        assert s.models.default == "org-pinned"
        assert s.permissions.mode == "dontAsk"

    def test_managed_missing_file_is_no_op(self, tmp_path, monkeypatch):
        # Point at a path that doesn't exist — loader should fall
        # back to lower tiers without raising.
        monkeypatch.setattr(
            "ember_code.core.config.settings._platform_managed_settings_path",
            lambda: tmp_path / "does-not-exist.yaml",
        )
        s = load_settings(
            cli_overrides={"models": {"default": "user-cli-choice"}},
            project_dir=tmp_path,
        )
        assert s.models.default == "user-cli-choice"

    def test_managed_partial_keys_preserve_other_layers(self, tmp_path, monkeypatch):
        """Managed only enforces what it sets — other fields fall
        through normally. A policy that pins ``permissions.mode``
        shouldn't wipe out the user's chosen model."""
        managed = tmp_path / "managed.yaml"
        managed.write_text("permissions:\n  mode: dontAsk\n")
        monkeypatch.setattr(
            "ember_code.core.config.settings._platform_managed_settings_path",
            lambda: managed,
        )
        s = load_settings(
            cli_overrides={"models": {"default": "user-cli-choice"}},
            project_dir=tmp_path,
        )
        assert s.permissions.mode == "dontAsk"  # from managed
        assert s.models.default == "user-cli-choice"  # from CLI, untouched

    def test_managed_json_content_parses(self, tmp_path, monkeypatch):
        """JSON is a strict subset of YAML — admins coming from
        CC's ``managed-settings.json`` can paste their JSON content
        into our ``managed-settings.yaml`` and it parses fine."""
        managed = tmp_path / "managed.yaml"
        managed.write_text(
            '{"permissions": {"mode": "dontAsk"}, "models": {"default": "json-model"}}'
        )
        monkeypatch.setattr(
            "ember_code.core.config.settings._platform_managed_settings_path",
            lambda: managed,
        )
        s = load_settings(project_dir=tmp_path)
        assert s.permissions.mode == "dontAsk"
        assert s.models.default == "json-model"

    def test_platform_returns_none_disables_managed(self, tmp_path, monkeypatch):
        """Unknown platform (no managed location defined) → managed
        tier is silently skipped without error."""
        monkeypatch.setattr(
            "ember_code.core.config.settings._platform_managed_settings_path",
            lambda: None,
        )
        s = load_settings(
            cli_overrides={"models": {"default": "cli-wins"}},
            project_dir=tmp_path,
        )
        assert s.models.default == "cli-wins"


class TestSettingsJsonLifted:
    """``load_settings`` lifts ``permissions`` out of ``settings.json``
    files so the ``PermissionEvaluator`` sees the same rules the
    user wrote in their CC-style config — without this, a ``deny``
    rule there silently never fired and bypass mode let through
    commands it should have blocked.

    Other keys (``hooks``) are owned by their dedicated loaders;
    only ``permissions`` is lifted into the unified ``Settings``.
    """

    def test_user_settings_json_deny_reaches_evaluator(self, tmp_path, monkeypatch) -> None:
        user_home = tmp_path / "home"
        ember_dir = user_home / ".ember"
        ember_dir.mkdir(parents=True)
        (ember_dir / "settings.json").write_text('{"permissions": {"deny": ["Bash(rm -rf /)"]}}')
        monkeypatch.setattr("pathlib.Path.home", lambda: user_home)

        s = load_settings(project_dir=tmp_path)
        assert "Bash(rm -rf /)" in s.permissions.deny

    def test_project_settings_json_overrides_user(self, tmp_path, monkeypatch) -> None:
        user_home = tmp_path / "home"
        (user_home / ".ember").mkdir(parents=True)
        (user_home / ".ember" / "settings.json").write_text(
            '{"permissions": {"deny": ["Bash(user-rule)"]}}'
        )
        project_ember = tmp_path / "proj" / ".ember"
        project_ember.mkdir(parents=True)
        (project_ember / "settings.json").write_text(
            '{"permissions": {"deny": ["Bash(project-rule)"]}}'
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: user_home)

        s = load_settings(project_dir=tmp_path / "proj")
        # ``_deep_merge`` replaces lists — matches the YAML
        # precedence. Project wins for the ``deny`` list.
        assert s.permissions.deny == ["Bash(project-rule)"]

    def test_missing_settings_json_is_silent(self, tmp_path, monkeypatch) -> None:
        user_home = tmp_path / "home"
        (user_home / ".ember").mkdir(parents=True)
        monkeypatch.setattr("pathlib.Path.home", lambda: user_home)

        s = load_settings(project_dir=tmp_path)
        assert s.permissions.deny == []

    def test_malformed_settings_json_does_not_crash(self, tmp_path, monkeypatch) -> None:
        # Best-effort: a hand-edited file with a syntax error
        # must NOT take down the BE on startup. Skip + defaults.
        user_home = tmp_path / "home"
        ember_dir = user_home / ".ember"
        ember_dir.mkdir(parents=True)
        (ember_dir / "settings.json").write_text("{not valid json")
        monkeypatch.setattr("pathlib.Path.home", lambda: user_home)

        s = load_settings(project_dir=tmp_path)
        assert s.permissions.deny == []

    def test_hooks_key_is_NOT_lifted(self, tmp_path, monkeypatch) -> None:
        # ``hooks`` has its own dedicated loader (HookLoader). If
        # ``load_settings`` also lifted it, the unified ``HooksConfig``
        # dataclass would either drop the unfamiliar shape or
        # there'd be two competing sources of truth. Confirm we
        # skip it entirely.
        user_home = tmp_path / "home"
        ember_dir = user_home / ".ember"
        ember_dir.mkdir(parents=True)
        (ember_dir / "settings.json").write_text(
            """{"hooks": {"PreToolUse": [{"type": "command", "command": "x"}]},
                "permissions": {"deny": ["Bash(rm *)"]}}"""
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: user_home)

        s = load_settings(project_dir=tmp_path)
        # Permissions lifted as expected …
        assert "Bash(rm *)" in s.permissions.deny
        # … but the hooks dict from settings.json didn't leak
        # through ``HooksConfig`` (it has only ``cross_tool_support``).
        assert not hasattr(s.hooks, "PreToolUse")
