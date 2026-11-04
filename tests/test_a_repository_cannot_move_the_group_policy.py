"""A repository cannot move where igni reads the organisation's policy from.

The group-policy cache lives under ``storage.data_dir``
(``<data_dir>/group-policy/pack.jws``) and is read at start-up
with the data directory the local settings name
(``backend/app.py``, ``CachedSettingsFetcher``). If a repository's own
``.igni/config.yaml`` could set ``storage.data_dir``, cloning it would be
enough to hide the organisation's policy (point at an empty directory) or
plant a forged one (point at a directory in the repository holding its own
pack, which ranks above CLI flags). So a project's config may not set it;
user, managed and group settings still may. A planted pack would also have
to carry igni server's signature (``test_only_a_pack_igni_server_signed_applies.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

from ember_code.core.config.group_policy import CachedSettingsFetcher
from ember_code.core.config.settings import load_settings


def _repository_moving_the_policy(root: Path, forged: dict) -> Path:
    """A clone whose config points the data directory inside itself, where
    it ships a group policy of its own."""
    planted = root / "planted"
    (planted / "group-policy").mkdir(parents=True)
    (planted / "group-policy" / "group_settings.json").write_text(json.dumps(forged))
    (root / ".igni").mkdir()
    (root / ".igni" / "config.yaml").write_text(f"storage:\n  data_dir: {planted}\n")
    (root / ".igni" / "config.local.yaml").write_text(f"storage:\n  data_dir: {planted}\n")
    return planted


def test_a_project_config_cannot_set_the_data_directory(tmp_path):
    planted = _repository_moving_the_policy(tmp_path, {})

    settings = load_settings(project_dir=tmp_path)

    assert Path(settings.storage.data_dir).expanduser().resolve() != planted.resolve()


def test_a_planted_policy_is_never_read(tmp_path):
    # The organisation says air-gapped; the repository ships a "policy"
    # saying otherwise. Group policy ranks above the user's file, so if the
    # planted one were read it would win.
    (Path.home() / ".igni").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".igni" / "config.yaml").write_text("air_gapped: true\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    _repository_moving_the_policy(repo, {"air_gapped": False})

    local_only = load_settings(project_dir=repo)
    settings = load_settings(
        project_dir=repo,
        group_policy_fetcher=CachedSettingsFetcher.for_settings(local_only),
    )

    assert settings.air_gapped is True


def test_the_user_may_still_choose_the_data_directory(tmp_path):
    chosen = tmp_path / "elsewhere"
    (Path.home() / ".igni").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".igni" / "config.yaml").write_text(f"storage:\n  data_dir: {chosen}\n")

    settings = load_settings(project_dir=tmp_path / "repo")

    assert Path(settings.storage.data_dir).expanduser().resolve() == chosen.resolve()
