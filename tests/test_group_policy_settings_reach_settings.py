"""A group's ``settings`` entries can reach ``Settings`` through the facade.

``GroupPolicyTier`` merges a pack's ``settings`` entries between the CLI and
the managed-policy file. ``SettingsLoader.load`` has taken a
``group_policy_fetcher`` since the tier was written — but ``load_settings``,
the facade every production caller uses, dropped the argument on the floor. So
even a caller that *wanted* org policy applied had no way to ask for it.

The first class below pins that seam. Before that fix
``load_settings(group_policy_fetcher=...)`` raised ``TypeError: load_settings()
got an unexpected keyword argument``.

The rest pins the other half — that *production* uses it. For a long time
nothing did, and nothing could: ``GroupPolicyCache.materialize`` never wrote
``kind == "settings"`` entries to disk, so between one process and the next
there was nothing for a fetcher to read. Now the cache persists the pack as
igni server signed it (``pack.jws``), ``CachedSettingsFetcher`` verifies and
reads it, and both
entry points that build a real ``Settings`` — ``BackendApp.run`` and
``load_settings_from_options`` — construct one. The end-to-end claim being
tested is the one an admin makes: publish ``{"learning": {"enabled": false}}``
and the session comes up with learning off.

The degradation cases matter as much as the happy one. This runs on the path
every session and every CLI invocation takes, so no cache, no group, a
truncated file, metadata that will not parse, and a nonsense data directory
must all mean "no policy" rather than "no igni".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ember_code.core.config.group_policy import (
    CachedSettingsFetcher,
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyPack,
    PackTrust,
)
from ember_code.core.config.settings import load_settings
from tests.group_policy_signing import ISSUER, sign_in, store


def _pack(payload: dict, *, enabled: bool = True) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Platform",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy",
                content=json.dumps(payload),
                content_type="json",
                enabled=enabled,
            )
        ],
    )


class TestTheFacadeCarriesTheFetcher:
    def test_an_admin_can_turn_learning_off(self, tmp_path: Path):
        """An admin turning learning off for a group."""
        pack = _pack({"learning": {"enabled": False}})

        settings = load_settings(project_dir=tmp_path, group_policy_fetcher=lambda: pack)

        assert settings.learning.enabled is False

    def test_a_group_tightening_beats_a_cli_flag(self, tmp_path: Path):
        """The tier sits above CLI on purpose — that is the whole point of an
        org policy — and the facade has to preserve it, not just the plan."""
        pack = _pack({"permissions": {"shell_execute": "deny"}})

        settings = load_settings(
            cli_overrides={"permissions": {"shell_execute": "allow"}},
            project_dir=tmp_path,
            group_policy_fetcher=lambda: pack,
        )

        assert settings.permissions.shell_execute == "deny"

    def test_a_group_cannot_loosen_what_the_cli_denied(self, tmp_path: Path):
        """The ratchet ``GroupPolicyTier`` documents, reached through the
        facade rather than by constructing the plan by hand."""
        pack = _pack({"permissions": {"shell_execute": "allow"}})

        settings = load_settings(
            cli_overrides={"permissions": {"shell_execute": "deny"}},
            project_dir=tmp_path,
            group_policy_fetcher=lambda: pack,
        )

        assert settings.permissions.shell_execute == "deny"

    def test_no_group_changes_nothing(self, tmp_path: Path):
        assert load_settings(
            project_dir=tmp_path, group_policy_fetcher=lambda: None
        ).learning.enabled

    def test_a_disabled_entry_is_ignored(self, tmp_path: Path):
        pack = _pack({"learning": {"enabled": False}}, enabled=False)

        assert load_settings(
            project_dir=tmp_path, group_policy_fetcher=lambda: pack
        ).learning.enabled


# ── The production path ────────────────────────────────────────────────
#
# Everything below goes through the disk cache rather than a lambda,
# because that is what production has: the fetch happens in the backend's
# ``AuthController`` with a bearer token, and the read happens at settings
# load, which runs before this process has one.


def _data_dir() -> Path:
    """The home config directory the suite's fake ``$HOME`` gives us."""
    return Path.home() / ".igni"


def _publish(payload: dict, *, group_name: str = "Platform", version: int | None = None) -> Path:
    """Act as an admin publishing a policy, and igni fetching it.

    The cache is left the way a verified hydrate leaves it — the pack
    materialised, and stored exactly as igni server signed it — with this
    install configured for that server and its person signed in. Settings
    are read back from the signed pack and nowhere else.
    """
    _data_dir().mkdir(parents=True, exist_ok=True)
    (_data_dir() / "config.yaml").write_text(f"api_url: {ISSUER}\n")
    sign_in(_data_dir() / "credentials.json")
    pack = GroupPolicyPack(
        group_id="g1",
        group_name=group_name,
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy",
                content=json.dumps(payload),
                content_type="json",
            )
        ]
        if payload
        else [],
    )
    store(_data_dir() / "group-policy", pack, version=version)
    return _data_dir() / "group-policy"


def _fetcher() -> CachedSettingsFetcher:
    """Built the way production builds it: from the local-only settings."""
    return CachedSettingsFetcher.for_settings(load_settings())


class TestTheCachedFetcher:
    """``CachedSettingsFetcher`` — the disk-backed half of the wiring."""

    def test_an_admin_turning_off_learning_is_obeyed(self, tmp_path: Path):
        """SWP-23, end to end: the published policy reaches ``Settings``."""
        _publish({"learning": {"enabled": False}})

        settings = load_settings(project_dir=tmp_path, group_policy_fetcher=_fetcher())

        assert settings.learning.enabled is False

    def test_a_policy_survives_the_process_that_fetched_it(self, tmp_path: Path):
        """The point of persisting the pack: the fetch and the read are in
        different processes, so the pack has to come back off disk."""
        _publish({"learning": {"enabled": False}})

        assert _fetcher()().to_settings_dict() == {"learning": {"enabled": False}}

    def test_it_says_in_the_log_that_a_policy_applied(self, tmp_path: Path, caplog):
        """A policy that applies silently is as hard to trust as one that
        does not apply at all — ops has to be able to tell which happened."""
        _publish({"learning": {"enabled": False}}, group_name="Legal")

        with caplog.at_level(logging.INFO, logger="ember_code.core.config.group_policy"):
            _fetcher()()

        assert any(
            "org group policy applied" in r.message and "Legal" in r.getMessage()
            for r in caplog.records
        )

    def test_the_cache_is_read_once_per_fetcher(self):
        """Memoized, so a second tier apply (or a second load) costs nothing
        and cannot disagree with the first."""
        cache_dir = _publish({"learning": {"enabled": False}})
        fetcher = _fetcher()
        first = fetcher()

        (cache_dir / GroupPolicyCache.SIGNED_PACK_FILE).unlink()

        assert fetcher() is first

    def test_withdrawing_a_policy_restores_the_default(self, tmp_path: Path):
        """An admin who deletes the settings entry gets the default back —
        the last policy does not stay in force forever."""
        _publish({"learning": {"enabled": False}}, version=1)
        _publish({}, version=2)

        settings = load_settings(project_dir=tmp_path, group_policy_fetcher=_fetcher())

        assert settings.learning.enabled is True

    def test_no_unsigned_settings_file_is_written(self):
        """Settings come back from the signed pack; a derived unsigned copy
        would be a second thing to forge."""
        cache_dir = _publish({"learning": {"enabled": False}})

        assert not (cache_dir / GroupPolicyCache.SETTINGS_FILE).exists()


class TestItDegradesInsteadOfFailing:
    """No org, no network, no cache, bad cache — none may break start-up."""

    def test_no_cache_at_all_is_the_ordinary_case(self, tmp_path: Path):
        """Everyone outside an org. Not an error, not a warning."""
        fetcher = _fetcher()

        assert fetcher() is None
        assert load_settings(project_dir=tmp_path, group_policy_fetcher=fetcher).learning.enabled

    def test_a_data_directory_that_does_not_exist(self, tmp_path: Path):
        fetcher = CachedSettingsFetcher(tmp_path / "nowhere")

        assert fetcher() is None
        assert load_settings(project_dir=tmp_path, group_policy_fetcher=fetcher)

    def test_a_truncated_pack_is_reported_not_raised(self, tmp_path: Path, caplog):
        """A crash mid-write leaves half a JWS. Settings still load, without
        the policy, and the log says the pack was ignored."""
        cache_dir = _publish({"learning": {"enabled": False}})
        path = cache_dir / GroupPolicyCache.SIGNED_PACK_FILE
        path.write_text(path.read_text()[:40], encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="ember_code.core.config.group_policy"):
            settings = load_settings(project_dir=tmp_path, group_policy_fetcher=_fetcher())

        assert settings.learning.enabled is True
        assert any("ignored" in r.getMessage() for r in caplog.records)

    def test_a_cache_holding_a_list_is_not_a_policy(self, tmp_path: Path):
        cache_dir = _publish({"learning": {"enabled": False}})
        (cache_dir / GroupPolicyCache.SIGNED_PACK_FILE).write_text("[1, 2, 3]", encoding="utf-8")

        assert _fetcher()() is None

    def test_a_policy_outlives_its_metadata(self, tmp_path: Path):
        """``pack_meta.json`` is identity for the FE, never input to the
        merge — losing it must not cost the group its policy."""
        cache_dir = _publish({"learning": {"enabled": False}})
        (cache_dir / "pack_meta.json").unlink()

        settings = load_settings(project_dir=tmp_path, group_policy_fetcher=_fetcher())

        assert settings.learning.enabled is False

    def test_unparseable_metadata_does_not_cost_the_policy(self, tmp_path: Path):
        cache_dir = _publish({"learning": {"enabled": False}})
        (cache_dir / "pack_meta.json").write_text(
            json.dumps({"group_id": "g1", "group_name": "P", "fetched_at": "not a date"}),
            encoding="utf-8",
        )

        settings = load_settings(project_dir=tmp_path, group_policy_fetcher=_fetcher())

        assert settings.learning.enabled is False

    def test_a_data_dir_that_is_not_a_path_falls_back_to_the_default(self, tmp_path: Path):
        """Callers read ``storage.data_dir`` off a ``Settings``; a stub one
        hands back an object. Better the default directory than a coerced
        path nobody writes to."""
        _publish({"learning": {"enabled": False}})
        trust = PackTrust.from_settings(load_settings())

        assert CachedSettingsFetcher(object(), trust=trust)() is not None


class TestTheEntryPointsBuildOne:
    """The decision this fix had to make: *where* the fetcher is built."""

    def test_the_backend_applies_group_policy_at_boot(self, tmp_path: Path):
        """``BackendApp`` — the process that owns the session, holds the
        token, and starts once. This is the settings object handed to
        ``BackendServer`` and to every runtime in the pool."""
        from ember_code.backend.app import BackendApp

        _publish({"learning": {"enabled": False}})
        app = BackendApp(
            socket_path=None,
            ws_port=None,
            project_dir=tmp_path,
            resume_session_id=None,
            additional_dirs=None,
        )

        assert app._load_settings_with_group_policy().learning.enabled is False

    def test_the_backend_still_boots_without_a_policy(self, tmp_path: Path):
        from ember_code.backend.app import BackendApp

        app = BackendApp(
            socket_path=None,
            ws_port=None,
            project_dir=tmp_path,
            resume_session_id=None,
            additional_dirs=None,
        )

        assert app._load_settings_with_group_policy().learning.enabled is True

    def test_the_cli_applies_group_policy_too(self):
        """``igni -m`` runs a real session in this process, so a policy that
        held in the app and not on the command line would be half-enforced."""
        from ember_code.cli.invocation import load_settings_from_options
        from ember_code.cli.options import CliOptions

        _publish({"learning": {"enabled": False}})

        assert load_settings_from_options(CliOptions()).learning.enabled is False

    def test_a_group_still_cannot_loosen_what_the_cli_denied(self):
        """The ratchet holds through the production path, not just the
        hand-built plan — this is the combination an operator relies on."""
        from ember_code.cli.invocation import load_settings_from_options
        from ember_code.cli.options import CliOptions

        _publish({"permissions": {"shell_execute": "allow"}})

        settings = load_settings_from_options(CliOptions(read_only=True))

        assert settings.permissions.shell_execute == "deny"
