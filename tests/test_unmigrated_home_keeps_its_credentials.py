"""An install that never migrated reads config and state from one directory.

``paths.home_config_dir()`` falls back to the pre-rename ``~/.ember`` while that
is the directory on disk, and the settings loader goes through it — so an
unmigrated install's *config* was found. Its credentials and state were not:
``storage.data_dir``, ``storage.audit_log`` and ``auth.credentials_file`` were
built from ``DEFAULT_DATA_DIR``, which is unconditionally ``~/.igni``.

The result is the exact failure ``dir_migration``'s own docstring says the
design exists to avoid — "a rename that half happens signs somebody out" — with
the added twist that here nothing half-happened: the rename simply never ran,
and the two halves of the codebase disagreed about where to look. The session
came up with the user's configured model and no token.

Before the fix every assertion in ``TestAnUnmigratedHome`` failed with the
literal string ``~/.igni``.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR, home_config_dir


def _home(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: root))


class TestAnUnmigratedHome:
    def test_state_lands_where_the_config_was_read_from(self, tmp_path: Path, monkeypatch):
        legacy = tmp_path / LEGACY_CONFIG_DIR
        legacy.mkdir()
        _home(monkeypatch, tmp_path)

        settings = Settings()

        assert home_config_dir() == legacy
        assert Path(settings.storage.data_dir) == legacy

    def test_the_credentials_file_is_the_one_that_exists(self, tmp_path: Path, monkeypatch):
        """The half that signs somebody out. ``credentials.json`` is in the
        legacy directory; pointing at ``~/.igni/credentials.json`` reports a
        working install as a fresh one."""
        legacy = tmp_path / LEGACY_CONFIG_DIR
        legacy.mkdir()
        (legacy / "credentials.json").write_text('{"access_token": "t"}', encoding="utf-8")
        _home(monkeypatch, tmp_path)

        settings = Settings()

        assert Path(settings.auth.credentials_file) == legacy / "credentials.json"
        assert Path(settings.auth.credentials_file).is_file()

    def test_the_audit_log_follows_too(self, tmp_path: Path, monkeypatch):
        (tmp_path / LEGACY_CONFIG_DIR).mkdir()
        _home(monkeypatch, tmp_path)

        assert Path(Settings().storage.audit_log) == tmp_path / LEGACY_CONFIG_DIR / "audit.log"

    def test_the_full_loader_agrees(self, tmp_path: Path, monkeypatch):
        """Not just the schema defaults — the merged ``Settings`` a session
        actually runs on."""
        home, project = tmp_path / "home", tmp_path / "proj"
        (home / LEGACY_CONFIG_DIR).mkdir(parents=True)
        project.mkdir()
        _home(monkeypatch, home)

        settings = load_settings(project_dir=project)

        assert Path(settings.storage.data_dir) == home / LEGACY_CONFIG_DIR


class TestAMigratedOrFreshHome:
    def test_the_new_name_is_used_when_it_exists(self, tmp_path: Path, monkeypatch):
        (tmp_path / CONFIG_DIR).mkdir()
        (tmp_path / LEGACY_CONFIG_DIR).mkdir()
        _home(monkeypatch, tmp_path)

        assert Path(Settings().storage.data_dir) == tmp_path / CONFIG_DIR

    def test_a_fresh_machine_gets_the_new_name(self, tmp_path: Path, monkeypatch):
        _home(monkeypatch, tmp_path)

        assert Path(Settings().storage.data_dir) == tmp_path / CONFIG_DIR

    def test_the_default_is_resolved_per_instance_not_at_import(self, tmp_path: Path, monkeypatch):
        """A module constant would freeze whichever answer was true when the
        module was first imported — which, for a directory that a migration
        renames underneath the process, is the wrong one exactly when it
        matters."""
        first, second = tmp_path / "a", tmp_path / "b"
        (first / LEGACY_CONFIG_DIR).mkdir(parents=True)
        (second / CONFIG_DIR).mkdir(parents=True)

        _home(monkeypatch, first)
        before = Settings().storage.data_dir
        _home(monkeypatch, second)
        after = Settings().storage.data_dir

        assert Path(before) == first / LEGACY_CONFIG_DIR
        assert Path(after) == second / CONFIG_DIR
