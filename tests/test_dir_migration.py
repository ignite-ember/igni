"""Moving ``.ember`` to ``.igni`` without losing anything.

The home directory holds ``credentials.json`` and ``client_state.db``,
so the failure modes are not cosmetic: a rename that half happens signs
somebody out, and a rename that never happens makes a working install
look brand new because everything is looking at a ``~/.igni`` that is
not there.

Most of these are refusals. That is the point — the migration's job is
to be boring in the ordinary case and to *decline* in every case it
cannot decide, because the fallback in ``paths`` means declining costs
nothing while guessing could cost somebody their state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ember_code.core.config.settings import load_settings
from ember_code.core.dir_migration import (
    migrate_directory,
    migrate_home,
    migrate_project,
)
from ember_code.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR, home_config_dir


def _legacy(parent: Path, **files: str) -> Path:
    directory = parent / LEGACY_CONFIG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return directory


class TestTheOrdinaryCase:
    def test_it_renames(self, tmp_path: Path):
        _legacy(tmp_path, **{"credentials.json": '{"token": "t"}'})
        outcome = migrate_directory(tmp_path)

        assert outcome.moved
        assert not (tmp_path / LEGACY_CONFIG_DIR).exists()
        assert (tmp_path / CONFIG_DIR / "credentials.json").read_text() == '{"token": "t"}'

    def test_nested_content_comes_with_it(self, tmp_path: Path):
        """A rename moves the tree; this pins that nothing is walked and
        selectively copied, which is where a partial migration would come
        from."""
        legacy = _legacy(tmp_path)
        (legacy / "group-policy" / "agents").mkdir(parents=True)
        (legacy / "group-policy" / "agents" / "reviewer.md").write_text("body")

        assert migrate_directory(tmp_path).moved
        assert (tmp_path / CONFIG_DIR / "group-policy" / "agents" / "reviewer.md").read_text() == "body"

    def test_nothing_to_do_is_not_a_failure(self, tmp_path: Path):
        outcome = migrate_directory(tmp_path)
        assert not outcome.moved
        assert outcome.reason == "no legacy directory"

    def test_running_twice_is_a_no_op(self, tmp_path: Path):
        _legacy(tmp_path, **{"a": "1"})
        assert migrate_directory(tmp_path).moved
        second = migrate_directory(tmp_path)

        assert not second.moved
        assert (tmp_path / CONFIG_DIR / "a").read_text() == "1"


class TestWhatItRefuses:
    def test_both_directories_present(self, tmp_path: Path):
        """The state it will not resolve.

        Two directories with overlapping state is something only a person
        can unpick, and "you have both" is a better outcome than a silent
        interleave. Both are left exactly as they were.
        """
        _legacy(tmp_path, **{"credentials.json": "old"})
        (tmp_path / CONFIG_DIR).mkdir()
        (tmp_path / CONFIG_DIR / "credentials.json").write_text("new")

        outcome = migrate_directory(tmp_path)

        assert not outcome.moved
        assert "leaving both alone" in (outcome.reason or "")
        assert (tmp_path / LEGACY_CONFIG_DIR / "credentials.json").read_text() == "old"
        assert (tmp_path / CONFIG_DIR / "credentials.json").read_text() == "new"

    def test_a_symlink_is_left_alone(self, tmp_path: Path):
        """``ember-server/.ember`` in this workspace is a symlink to
        ``ember-code/.ember``. Renaming through it would move somebody
        else's directory and leave a dangling link behind."""
        real = tmp_path / "elsewhere"
        real.mkdir()
        (real / "credentials.json").write_text("shared")
        parent = tmp_path / "proj"
        parent.mkdir()
        (parent / LEGACY_CONFIG_DIR).symlink_to(real)

        outcome = migrate_directory(parent)

        assert not outcome.moved
        assert "symlink" in (outcome.reason or "")
        assert (parent / LEGACY_CONFIG_DIR).is_symlink()
        assert (real / "credentials.json").read_text() == "shared"

    def test_a_file_where_a_directory_should_be(self, tmp_path: Path):
        (tmp_path / LEGACY_CONFIG_DIR).write_text("not a directory")
        outcome = migrate_directory(tmp_path)

        assert not outcome.moved
        assert "not a directory" in (outcome.reason or "")
        assert (tmp_path / LEGACY_CONFIG_DIR).is_file()

    def test_an_unrenamable_directory_does_not_raise(self, tmp_path: Path, monkeypatch):
        """A migration that cannot run must not stop a session starting.
        The legacy directory stays and the next run tries again."""
        _legacy(tmp_path, **{"a": "1"})

        def boom(self, target):
            raise OSError("cross-device link")

        monkeypatch.setattr(Path, "rename", boom)
        outcome = migrate_directory(tmp_path)

        assert not outcome.moved
        assert "could not rename" in (outcome.reason or "")
        assert (tmp_path / LEGACY_CONFIG_DIR / "a").read_text() == "1"


class TestTheFallbackThatMakesItOptional:
    def test_the_legacy_directory_is_found_when_the_new_one_is_absent(
        self, tmp_path: Path, monkeypatch
    ):
        """The reason the rename is tidiness rather than correctness.

        A machine where the migration never ran — across filesystems, say
        — still has to find its credentials.
        """
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        _legacy(tmp_path, **{"credentials.json": "t"})

        assert home_config_dir() == tmp_path / LEGACY_CONFIG_DIR

    def test_the_new_directory_wins_when_both_exist(self, tmp_path: Path, monkeypatch):
        """The state the migration refuses to resolve still needs one
        answer, and it should be the new name."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        _legacy(tmp_path)
        (tmp_path / CONFIG_DIR).mkdir()

        assert home_config_dir() == tmp_path / CONFIG_DIR

    def test_a_fresh_machine_gets_the_new_name(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert home_config_dir() == tmp_path / CONFIG_DIR


class TestTheSettingsLoaderSeesAnUnmigratedInstall:
    """The reader that could not, and failed silently.

    ``SettingsMergePlan.default`` built its tier paths by hand —
    ``Path.home() / CONFIG_DIR`` — instead of going through the helpers,
    so it was the one place the fallback did not reach. And it is the
    worst place for that: a tier whose file is absent is indistinguishable
    from a tier with nothing set, so an unmigrated install came up on the
    built-in defaults — wrong model, wrong guardrails — without a word.

    The project case has a genuine ordering hazard behind it. Settings are
    loaded before a Session exists, and the project migration runs inside
    ``ProjectInitializer`` during Session construction, so on the first
    run after an upgrade there is no ``.igni`` to read yet.
    """

    def _config(self, directory: Path, model: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.yaml").write_text(
            yaml.safe_dump({"models": {"default": model}}), encoding="utf-8"
        )

    def test_a_project_config_under_the_old_name_is_still_read(self, tmp_path: Path):
        self._config(tmp_path / LEGACY_CONFIG_DIR, "configured-model")
        assert load_settings(project_dir=tmp_path).models.default == "configured-model"

    def test_a_project_config_under_the_new_name_is_read(self, tmp_path: Path):
        self._config(tmp_path / CONFIG_DIR, "configured-model")
        assert load_settings(project_dir=tmp_path).models.default == "configured-model"

    def test_the_new_name_wins_for_a_project(self, tmp_path: Path):
        self._config(tmp_path / LEGACY_CONFIG_DIR, "old")
        self._config(tmp_path / CONFIG_DIR, "new")
        assert load_settings(project_dir=tmp_path).models.default == "new"

    def test_a_home_config_under_the_old_name_is_still_read(
        self, tmp_path: Path, monkeypatch
    ):
        home, project = tmp_path / "home", tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        self._config(home / LEGACY_CONFIG_DIR, "from-old-home")

        assert load_settings(project_dir=project).models.default == "from-old-home"


class TestTheNamedWrappers:
    def test_home_migrates_the_home_directory(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        _legacy(tmp_path, **{"credentials.json": "t"})

        assert migrate_home().moved
        assert (tmp_path / CONFIG_DIR / "credentials.json").read_text() == "t"

    def test_project_migrates_the_project_directory(self, tmp_path: Path):
        _legacy(tmp_path, **{"config.yaml": "models: {}"})
        assert migrate_project(tmp_path).moved
        assert (tmp_path / CONFIG_DIR / "config.yaml").exists()

    @pytest.mark.parametrize("fn", [migrate_home, migrate_project])
    def test_neither_raises_on_an_impossible_parent(self, fn, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nope"))
        outcome = fn(tmp_path / "nope") if fn is migrate_project else fn()
        assert not outcome.moved


class TestTheConstants:
    def test_the_two_names_differ(self):
        assert CONFIG_DIR != LEGACY_CONFIG_DIR

    def test_the_new_name_is_the_product_name(self):
        assert CONFIG_DIR == ".igni"

    def test_the_legacy_name_is_what_it_was(self):
        # Spelled out on purpose. This is the one place the old name is a
        # fact about the outside world rather than a value to follow: it
        # is what is already on people's disks, so it cannot be rewritten
        # to track the constant.
        assert LEGACY_CONFIG_DIR == ".ember"
