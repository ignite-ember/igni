"""Tests for config/settings.py."""

from ember_code.core.config.settings import (
    ModelsConfig,
    PermissionsConfig,
    Settings,
    _deep_merge,
    _load_yaml,
    load_settings,
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
