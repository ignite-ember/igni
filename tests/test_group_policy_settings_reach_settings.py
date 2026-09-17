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
there was nothing for a fetcher to read. Now the cache persists them
(``group_settings.json``), ``CachedSettingsFetcher`` reads them, and both
entry points that build a real ``Settings`` — ``BackendApp.run`` and
``load_settings_from_options`` — construct one. The end-to-end claim being
tested is the one an admin makes: publish ``{"code_index": {"enabled": false}}``
and the session comes up with the code index off.

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
)
from ember_code.core.config.settings import load_settings


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
    def test_an_admin_can_turn_the_code_index_off(self, tmp_path: Path):
        """The example the ``code_index.enabled`` docstring gives."""
        pack = _pack({"code_index": {"enabled": False}})

        settings = load_settings(project_dir=tmp_path, group_policy_fetcher=lambda: pack)

        assert settings.code_index.enabled is False

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
        ).code_index.enabled

    def test_a_disabled_entry_is_ignored(self, tmp_path: Path):
        pack = _pack({"code_index": {"enabled": False}}, enabled=False)

        assert load_settings(
            project_dir=tmp_path, group_policy_fetcher=lambda: pack
        ).code_index.enabled


# ── The production path ────────────────────────────────────────────────
#
# Everything below goes through the disk cache rather than a lambda,
# because that is what production has: the fetch happens in the backend's
# ``AuthController`` with a bearer token, and the read happens at settings
# load, which runs before this process has one.


def _data_dir() -> Path:
    """The home config directory the suite's fake ``$HOME`` gives us."""
    return Path.home() / ".igni"


def _publish(payload: dict, *, group_name: str = "Platform") -> GroupPolicyCache:
    """Act as an admin publishing a policy: materialise it into the cache.

    Deliberately the real :meth:`GroupPolicyCache.materialize`, not a
    hand-written file. The claim under test spans the writer and the reader,
    and a test that wrote ``group_settings.json`` itself would still pass on
    the day ``_write_settings`` stopped writing it.
    """
    cache = GroupPolicyCache(cache_dir=_data_dir() / "group-policy")
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
        ],
    )
    cache.materialize(pack)
    return cache


class TestTheCachedFetcher:
    """``CachedSettingsFetcher`` — the disk-backed half of the wiring."""

    def test_an_admin_turning_off_the_code_index_is_obeyed(self, tmp_path: Path):
        """SWP-23, end to end: the published policy reaches ``Settings``."""
        _publish({"code_index": {"enabled": False}})

        settings = load_settings(
            project_dir=tmp_path,
            group_policy_fetcher=CachedSettingsFetcher(_data_dir()),
        )

        assert settings.code_index.enabled is False

    def test_a_policy_survives_the_process_that_fetched_it(self, tmp_path: Path):
        """The point of persisting settings: the fetch and the read are in
        different processes, so the pack has to come back off disk."""
        _publish({"code_index": {"enabled": False}})

        # A fetcher built from nothing but the data directory — no pack
        # object handed over, no network, no token.
        assert CachedSettingsFetcher(_data_dir())().to_settings_dict() == {
            "code_index": {"enabled": False}
        }

    def test_it_says_in_the_log_that_a_policy_applied(self, tmp_path: Path, caplog):
        """A policy that applies silently is as hard to trust as one that
        does not apply at all — ops has to be able to tell which happened."""
        _publish({"code_index": {"enabled": False}}, group_name="Legal")

        with caplog.at_level(logging.INFO, logger="ember_code.core.config.group_policy"):
            CachedSettingsFetcher(_data_dir())()

        assert any(
            "org group policy applied" in r.message and "Legal" in r.getMessage()
            for r in caplog.records
        )

    def test_the_cache_is_read_once_per_fetcher(self):
        """Memoized, so a second tier apply (or a second load) costs nothing
        and cannot disagree with the first."""
        _publish({"code_index": {"enabled": False}})
        fetcher = CachedSettingsFetcher(_data_dir())
        first = fetcher()

        (_data_dir() / "group-policy" / GroupPolicyCache.SETTINGS_FILE).unlink()

        assert fetcher() is first

    def test_withdrawing_a_policy_restores_the_default(self, tmp_path: Path):
        """An admin who deletes the settings entry gets the default back —
        the last policy does not stay in force forever."""
        cache = _publish({"code_index": {"enabled": False}})
        cache.materialize(
            GroupPolicyPack(
                group_id="g1",
                group_name="Platform",
                fetched_at=datetime.now(timezone.utc),
                entries=[],
            )
        )

        settings = load_settings(
            project_dir=tmp_path,
            group_policy_fetcher=CachedSettingsFetcher(_data_dir()),
        )

        assert settings.code_index.enabled is True


class TestItDegradesInsteadOfFailing:
    """No org, no network, no cache, bad cache — none may break start-up."""

    def test_no_cache_at_all_is_the_ordinary_case(self, tmp_path: Path):
        """Everyone outside an org. Not an error, not a warning."""
        fetcher = CachedSettingsFetcher(_data_dir())

        assert fetcher() is None
        assert load_settings(project_dir=tmp_path, group_policy_fetcher=fetcher).code_index.enabled

    def test_a_data_directory_that_does_not_exist(self, tmp_path: Path):
        fetcher = CachedSettingsFetcher(tmp_path / "nowhere")

        assert fetcher() is None
        assert load_settings(project_dir=tmp_path, group_policy_fetcher=fetcher)

    def test_a_truncated_cache_file_is_reported_not_raised(self, tmp_path: Path, caplog):
        """A crash mid-write leaves half a JSON object. Settings still load,
        and the log distinguishes this from 'your group publishes nothing'."""
        _publish({"code_index": {"enabled": False}})
        path = _data_dir() / "group-policy" / GroupPolicyCache.SETTINGS_FILE
        path.write_text('{"code_index": {"enab', encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="ember_code.core.config.group_policy"):
            settings = load_settings(
                project_dir=tmp_path,
                group_policy_fetcher=CachedSettingsFetcher(_data_dir()),
            )

        assert settings.code_index.enabled is True
        assert any("malformed" in r.getMessage() for r in caplog.records)

    def test_a_cache_holding_a_list_is_not_a_policy(self, tmp_path: Path):
        _publish({"code_index": {"enabled": False}})
        path = _data_dir() / "group-policy" / GroupPolicyCache.SETTINGS_FILE
        path.write_text("[1, 2, 3]", encoding="utf-8")

        assert CachedSettingsFetcher(_data_dir())() is None

    def test_a_policy_outlives_its_metadata(self, tmp_path: Path):
        """``pack_meta.json`` is identity for the log line and the FE, never
        input to the merge — losing it must not cost the group its policy."""
        _publish({"code_index": {"enabled": False}})
        (_data_dir() / "group-policy" / "pack_meta.json").unlink()

        settings = load_settings(
            project_dir=tmp_path,
            group_policy_fetcher=CachedSettingsFetcher(_data_dir()),
        )

        assert settings.code_index.enabled is False

    def test_unparseable_metadata_does_not_cost_the_policy(self, tmp_path: Path):
        _publish({"code_index": {"enabled": False}})
        (_data_dir() / "group-policy" / "pack_meta.json").write_text(
            json.dumps({"group_id": "g1", "group_name": "P", "fetched_at": "not a date"}),
            encoding="utf-8",
        )

        settings = load_settings(
            project_dir=tmp_path,
            group_policy_fetcher=CachedSettingsFetcher(_data_dir()),
        )

        assert settings.code_index.enabled is False

    def test_a_data_dir_that_is_not_a_path_falls_back_to_the_default(self, tmp_path: Path):
        """Callers read ``storage.data_dir`` off a ``Settings``; a stub one
        hands back an object. Better the default directory than a coerced
        path nobody writes to."""
        _publish({"code_index": {"enabled": False}})

        assert CachedSettingsFetcher(object())() is not None


class TestTheEntryPointsBuildOne:
    """The decision this fix had to make: *where* the fetcher is built."""

    def test_the_backend_applies_group_policy_at_boot(self, tmp_path: Path):
        """``BackendApp`` — the process that owns the session, holds the
        token, and starts once. This is the settings object handed to
        ``BackendServer`` and to every runtime in the pool."""
        from ember_code.backend.app import BackendApp

        _publish({"code_index": {"enabled": False}})
        app = BackendApp(
            socket_path=None,
            ws_port=None,
            project_dir=tmp_path,
            resume_session_id=None,
            additional_dirs=None,
        )

        assert app._load_settings_with_group_policy().code_index.enabled is False

    def test_the_backend_still_boots_without_a_policy(self, tmp_path: Path):
        from ember_code.backend.app import BackendApp

        app = BackendApp(
            socket_path=None,
            ws_port=None,
            project_dir=tmp_path,
            resume_session_id=None,
            additional_dirs=None,
        )

        assert app._load_settings_with_group_policy().code_index.enabled is True

    def test_the_cli_applies_group_policy_too(self):
        """``igni -m`` runs a real session in this process, so a policy that
        held in the app and not on the command line would be half-enforced."""
        from ember_code.cli.invocation import load_settings_from_options
        from ember_code.cli.options import CliOptions

        _publish({"code_index": {"enabled": False}})

        assert load_settings_from_options(CliOptions()).code_index.enabled is False

    def test_a_group_still_cannot_loosen_what_the_cli_denied(self):
        """The ratchet holds through the production path, not just the
        hand-built plan — this is the combination an operator relies on."""
        from ember_code.cli.invocation import load_settings_from_options
        from ember_code.cli.options import CliOptions

        _publish({"permissions": {"shell_execute": "allow"}})

        settings = load_settings_from_options(CliOptions(read_only=True))

        assert settings.permissions.shell_execute == "deny"
