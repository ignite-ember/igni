"""The credential scanner has to look wherever the loader looks.

``scan_project_config`` warned about literal ``api_key`` values in the
project's shareable ``config.yaml``, and only ever built one path:
``<project>/.igni/config.yaml``. The settings loader is not so narrow —
:func:`ember_code.core.paths.project_config_dir` falls back to the
pre-rename ``.ember`` while that is the directory on disk, and
``SettingsMergePlan.standard`` builds every project tier through it. So a
project that had not migrated was a fully working install whose config
was read on every session and scanned on none of them: exactly the
install least likely to have been audited, silently exempt.

Committing is the part that matters, not loading. A key in
``.ember/config.yaml`` ships to the remote whether or not anything
parses it, which is why both directories are scanned even though the
loader only ever reads one of them.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ember_code.core.config.secret_scan import scan_project_config
from ember_code.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR


def _write_key(project_dir: Path, config_dir: str, model: str, key: str) -> None:
    path = project_dir / config_dir / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"models": {"registry": {model: {"model_id": "m", "api_key": key}}}}),
        encoding="utf-8",
    )


class TestTheLegacyDirectoryIsScanned:
    def test_a_key_in_the_legacy_dir_is_found(self, tmp_path: Path):
        """The whole finding, in one assertion: this returned ``[]``."""
        _write_key(tmp_path, LEGACY_CONFIG_DIR, "m3", "sk-oops")

        warnings = scan_project_config(tmp_path)

        assert len(warnings) == 1
        assert "m3" in warnings[0]
        assert "api_key_env" in warnings[0]

    def test_the_warning_names_the_directory_it_found(self, tmp_path: Path):
        """Two candidate directories means ``config.yaml`` alone no
        longer identifies a file."""
        _write_key(tmp_path, LEGACY_CONFIG_DIR, "m3", "sk-oops")

        assert f"{LEGACY_CONFIG_DIR}/config.yaml" in scan_project_config(tmp_path)[0]

    def test_the_secret_is_still_never_quoted(self, tmp_path: Path):
        """The new path must not become the one that logs the key."""
        _write_key(tmp_path, LEGACY_CONFIG_DIR, "m3", "sk-do-not-log-me")

        assert "sk-do-not-log-me" not in scan_project_config(tmp_path)[0]

    def test_both_directories_are_reported_when_both_carry_a_key(self, tmp_path: Path):
        """``project_config_dir`` prefers the new name and reads only
        that one — but the legacy file is still in the repository, so
        the scanner is deliberately wider than the loader here."""
        _write_key(tmp_path, CONFIG_DIR, "new", "sk-1")
        _write_key(tmp_path, LEGACY_CONFIG_DIR, "old", "sk-2")

        warnings = scan_project_config(tmp_path)

        assert len(warnings) == 2
        joined = " ".join(warnings)
        assert "'new'" in joined and "'old'" in joined

    def test_a_clean_legacy_config_says_nothing(self, tmp_path: Path):
        """Widening the scan must not make the migration itself a
        warning — only a literal key is."""
        path = tmp_path / LEGACY_CONFIG_DIR / "config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            yaml.safe_dump({"models": {"registry": {"m": {"api_key_env": "MY_KEY"}}}}),
            encoding="utf-8",
        )

        assert scan_project_config(tmp_path) == []

    def test_an_unparseable_legacy_config_is_not_an_error(self, tmp_path: Path):
        """And it must not stop the new directory being scanned — a
        broken legacy file used to be the only file, so skipping the
        whole scan was indistinguishable from finding nothing."""
        broken = tmp_path / LEGACY_CONFIG_DIR / "config.yaml"
        broken.parent.mkdir(parents=True)
        broken.write_text("models: [this is not: valid: yaml", encoding="utf-8")
        _write_key(tmp_path, CONFIG_DIR, "m3", "sk-oops")

        warnings = scan_project_config(tmp_path)

        assert len(warnings) == 1
        assert "m3" in warnings[0]

    def test_the_legacy_local_file_is_still_not_scanned(self, tmp_path: Path):
        """``config.local.yaml`` is gitignored in either directory."""
        path = tmp_path / LEGACY_CONFIG_DIR / "config.local.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            yaml.safe_dump({"models": {"registry": {"m": {"api_key": "sk-mine"}}}}),
            encoding="utf-8",
        )

        assert scan_project_config(tmp_path) == []
