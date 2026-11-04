"""The permission settings chain must see an unmigrated project.

``merge_plan.build_default`` resolves the project's config directory
through :func:`ember_code.core.paths.project_config_dir`, which answers
``<project>/.ember`` while that is the directory actually on disk. It
then loads ``<dir>/settings.json`` and ``<dir>/settings.local.json`` as
JSON fragment tiers.

``SettingsFileLoader`` loads *the same two files* for the tool-permission
store, and used to join ``CONFIG_DIR`` (``.igni``) by hand. So on a
project that had not been migrated, ``Settings`` read the settings file
and the permission store did not — and what that file mostly carries is
``permissions.deny``. The disagreement failed open.

These tests pin both halves: the reader finds the legacy file, and the
writer does not escape to the new directory behind the reader's back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ember_code.core.config.tool_permissions.settings_files import (
    SettingsFileLoader,
    SettingsFileWriter,
)
from ember_code.core.paths import project_config_dir


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory with neither config dir in it, so the two home
    tiers contribute nothing and the assertions below are about the
    project tiers alone (without this the developer's real
    ``~/.igni/settings.local.json`` shows up in ``load_all``)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _write_deny(config_dir: Path, rule: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(
        json.dumps({"permissions": {"deny": [rule]}}),
    )


class TestLegacyProjectDirIsRead:
    def test_loader_reads_legacy_settings_json(self, tmp_path: Path, isolated_home: Path) -> None:
        """A project holding only ``.ember/`` still has its deny rule
        loaded. Before the fix ``load_all()`` returned no project result
        at all: the loader looked only under ``.igni``."""
        project = tmp_path / "proj"
        _write_deny(project / ".ember", "Bash(rm -rf *)")

        results = SettingsFileLoader(project).load_all()

        denies = [rule for r in results if r.file for rule in r.file.deny]
        assert "Bash(rm -rf *)" in denies

    def test_loader_agrees_with_the_settings_merge_plan(
        self, tmp_path: Path, isolated_home: Path
    ) -> None:
        """The two readers of these files must resolve the same
        directory — that identity is the invariant, not the literal
        name, so it survives the next rename too."""
        project = tmp_path / "proj"
        (project / ".ember").mkdir(parents=True)

        project_paths = [p for p in SettingsFileLoader(project).paths() if project in p.parents]

        assert project_paths == [
            project_config_dir(project) / "settings.json",
            project_config_dir(project) / "settings.local.json",
        ]

    def test_new_dir_still_wins_when_both_exist(self, tmp_path: Path, isolated_home: Path) -> None:
        """``project_config_dir`` prefers ``.igni`` when both are on
        disk — the state the directory migration refuses to resolve.
        Pinned here so the fallback cannot turn into "prefer legacy"."""
        project = tmp_path / "proj"
        _write_deny(project / ".ember", "Bash(legacy)")
        _write_deny(project / ".igni", "Bash(current)")

        results = SettingsFileLoader(project).load_all()

        denies = [rule for r in results if r.file for rule in r.file.deny]
        assert denies == ["Bash(current)"]


class TestWriterFollowsTheSameDirectory:
    def test_writer_targets_the_legacy_dir_when_that_is_the_live_one(
        self, tmp_path: Path, isolated_home: Path
    ) -> None:
        """Persisting an "always allow" must land where the loader will
        look. Before the fix it created ``.igni/settings.local.json``,
        which then *became* the preferred directory — so the rules in
        ``.ember/`` went dark on the very next session."""
        project = tmp_path / "proj"
        _write_deny(project / ".ember", "Bash(rm -rf *)")

        written = SettingsFileWriter(project).save_rule("Bash(git status)", "allow")

        assert written == project / ".ember" / "settings.local.json"
        assert not (project / ".igni").exists()

        results = SettingsFileLoader(project).load_all()
        allows = [rule for r in results if r.file for rule in r.file.allow]
        assert "Bash(git status)" in allows

    def test_writer_falls_back_to_home_without_a_project(
        self, tmp_path: Path, isolated_home: Path
    ) -> None:
        """No project dir means the user-global file, resolved through
        the same helper so an unmigrated ``~/.ember`` is honoured."""
        (isolated_home / ".ember").mkdir()

        target = SettingsFileWriter(None).target_path()

        assert target == isolated_home / ".ember" / "settings.local.json"
