"""Credentials belong in the home directory, definitions in the project.

The project's config carries provider definitions — which models exist,
their URLs, their context windows — and is on its way to being
committed. The keys that reach them are not shareable, and the existing
``.local.`` filename convention only protects them because the whole
config directory is gitignored today.

Two things make the split work, and both are tested here.

**The merge already does it.** Config tiers deep-merge per field, so a
home config carrying nothing but ``api_key`` combines with a project
config carrying everything else. That is asserted rather than assumed,
because the whole design rests on it: if the tiers replaced whole
registry entries instead, splitting a provider across two files would
silently lose half of it.

**A missing key has to say so.** ``provider_builders`` passes the
literal string ``"not-set"`` when nothing resolves, so construction
succeeds and the *first call* fails with the provider's own
authentication error — phrased in their terms, arriving nowhere near the
cause. Splitting the config makes that far more likely to be hit: a
project config can now name a provider whose key is meant to live in
somebody's home directory, and a teammate who has not set theirs up hits
exactly this.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ember_code.core.config.model_entry import ModelRegistryEntry
from ember_code.core.config.secret_scan import scan_project_config
from ember_code.core.paths import CONFIG_DIR


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


class TestTheMergeTheSplitRestsOn:
    """A key in the home config, a definition in the project's."""

    def test_a_home_key_joins_a_project_definition(self, tmp_path: Path, monkeypatch):
        home, project = tmp_path / "home", tmp_path / "proj"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        _write(
            home / CONFIG_DIR / "config.yaml",
            {"models": {"registry": {"m3": {"api_key": "sk-from-home"}}}},
        )
        _write(
            project / CONFIG_DIR / "config.yaml",
            {
                "models": {
                    "registry": {
                        "m3": {
                            "provider": "openai_like",
                            "model_id": "MiniMax-M3",
                            "url": "https://api.minimax.io/v1",
                            "context_window": 204800,
                        }
                    }
                }
            },
        )

        from ember_code.core.config.settings import load_settings

        settings = load_settings(project_dir=project)
        entry = settings.models.registry["m3"]
        data = entry if isinstance(entry, dict) else entry.model_dump()

        # Both halves survived. If tiers replaced whole entries rather
        # than merging fields, one of these would be missing and the
        # split would be silently impossible.
        assert data["model_id"] == "MiniMax-M3"
        assert data["url"] == "https://api.minimax.io/v1"
        assert data["api_key"] == "sk-from-home"

    def test_the_project_still_wins_a_shared_field(self, tmp_path: Path, monkeypatch):
        """Merging must not invert the precedence it merges into."""
        home, project = tmp_path / "home", tmp_path / "proj"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        _write(
            home / CONFIG_DIR / "config.yaml",
            {"models": {"registry": {"m3": {"api_key": "k", "context_window": 1000}}}},
        )
        # ``model_id`` is required, and it lives in the project half —
        # which is the right way round: it is part of the definition, not
        # the credential. The home half can be key-only because of it.
        _write(
            project / CONFIG_DIR / "config.yaml",
            {"models": {"registry": {"m3": {"model_id": "m", "context_window": 204800}}}},
        )

        from ember_code.core.config.settings import load_settings

        settings = load_settings(project_dir=project)
        entry = settings.models.registry["m3"]
        data = entry if isinstance(entry, dict) else entry.model_dump()
        assert data["context_window"] == 204800
        assert data["api_key"] == "k"


class TestAMissingKeySaysSo:
    def test_it_names_the_model_and_what_to_do(self):
        entry = ModelRegistryEntry(model_id="MiniMax-M3", provider="openai_like")
        message = entry.why_no_api_key()

        assert "MiniMax-M3" in message
        # The two shareable ways to reach a secret, since that is the
        # answer somebody needs rather than the diagnosis.
        assert "api_key_env" in message
        assert "config.yaml" in message

    def test_it_names_the_environment_variable_it_tried(self):
        entry = ModelRegistryEntry(
            model_id="m", provider="openai_like", api_key_env="MINIMAX_API_KEY"
        )
        assert "MINIMAX_API_KEY" in entry.why_no_api_key()

    def test_it_names_the_command_it_tried(self):
        entry = ModelRegistryEntry(
            model_id="m", provider="openai_like", api_key_cmd="security find-key"
        )
        assert "security find-key" in entry.why_no_api_key()

    def test_it_mentions_signing_in_for_a_cloud_model(self):
        """A cloud-gateway URL with no token is not a misconfiguration,
        it is somebody who has not logged in — different fix."""
        entry = ModelRegistryEntry(
            model_id="m", provider="openai_like", url="https://api.ignite-ember.sh/v1"
        )
        assert "signed in" in entry.why_no_api_key()

    def test_a_resolvable_key_needs_no_explanation(self, monkeypatch):
        """The diagnostic exists for the failure; it should not be
        consulted on the happy path."""
        monkeypatch.setenv("SOME_KEY", "sk-real")
        entry = ModelRegistryEntry(
            model_id="m", provider="openai_like", api_key_env="SOME_KEY"
        )
        assert entry.resolve_api_key() == "sk-real"


class TestTheSecretScan:
    def test_it_finds_a_literal_key_in_the_shared_config(self, tmp_path: Path):
        _write(
            tmp_path / CONFIG_DIR / "config.yaml",
            {"models": {"registry": {"m3": {"api_key": "sk-oops", "model_id": "m"}}}},
        )
        warnings = scan_project_config(tmp_path)

        assert len(warnings) == 1
        assert "m3" in warnings[0]
        assert "config.yaml" in warnings[0]
        # Says what to do, not just what is wrong.
        assert "api_key_env" in warnings[0]

    def test_it_says_nothing_about_a_definition_with_no_key(self, tmp_path: Path):
        _write(
            tmp_path / CONFIG_DIR / "config.yaml",
            {"models": {"registry": {"m3": {"model_id": "m", "api_key_env": "K"}}}},
        )
        assert scan_project_config(tmp_path) == []

    def test_api_key_env_is_not_a_secret(self, tmp_path: Path):
        """It names one. That is the whole point of recommending it."""
        _write(
            tmp_path / CONFIG_DIR / "config.yaml",
            {"models": {"registry": {"m": {"api_key_env": "MY_KEY", "api_key_cmd": "get-key"}}}},
        )
        assert scan_project_config(tmp_path) == []

    def test_the_cloud_sentinel_is_not_a_secret(self, tmp_path: Path):
        """``cloud_token`` is an instruction to go and find one."""
        _write(
            tmp_path / CONFIG_DIR / "config.yaml",
            {"models": {"registry": {"m": {"api_key": "cloud_token"}}}},
        )
        assert scan_project_config(tmp_path) == []

    def test_the_local_file_is_not_scanned(self, tmp_path: Path):
        """``config.local.yaml`` is a person's own and gitignored, so a
        key there is a choice rather than a mistake."""
        _write(
            tmp_path / CONFIG_DIR / "config.local.yaml",
            {"models": {"registry": {"m": {"api_key": "sk-mine"}}}},
        )
        assert scan_project_config(tmp_path) == []

    def test_no_config_at_all(self, tmp_path: Path):
        assert scan_project_config(tmp_path) == []

    def test_unparseable_config_is_not_an_error(self, tmp_path: Path):
        """A broken config fails loudly elsewhere; this must not be the
        thing that stops a session starting."""
        path = tmp_path / CONFIG_DIR / "config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("models: [this is not: valid: yaml", encoding="utf-8")
        assert scan_project_config(tmp_path) == []

    @pytest.mark.parametrize("payload", [None, [], "a string", {"models": None}, {"models": {"registry": []}}])
    def test_shapes_that_are_not_a_registry(self, tmp_path: Path, payload):
        path = tmp_path / CONFIG_DIR / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        assert scan_project_config(tmp_path) == []

    def test_it_names_every_offender(self, tmp_path: Path):
        _write(
            tmp_path / CONFIG_DIR / "config.yaml",
            {
                "models": {
                    "registry": {
                        "a": {"api_key": "sk-1"},
                        "b": {"api_key": "sk-2"},
                        "c": {"api_key_env": "FINE"},
                    }
                }
            },
        )
        warning = scan_project_config(tmp_path)[0]
        assert "'a'" in warning and "'b'" in warning
        assert "'c'" not in warning

    def test_the_key_itself_is_never_in_the_warning(self, tmp_path: Path):
        """A warning that quotes the secret puts it in the logs."""
        _write(
            tmp_path / CONFIG_DIR / "config.yaml",
            {"models": {"registry": {"m": {"api_key": "sk-do-not-log-me"}}}},
        )
        assert "sk-do-not-log-me" not in scan_project_config(tmp_path)[0]
