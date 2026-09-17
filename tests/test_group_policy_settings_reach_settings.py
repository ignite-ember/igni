"""A group's ``settings`` entries can reach ``Settings`` through the facade.

``GroupPolicyTier`` merges a pack's ``settings`` entries between the CLI and
the managed-policy file. ``SettingsLoader.load`` has taken a
``group_policy_fetcher`` since the tier was written — but ``load_settings``,
the facade every production caller uses, dropped the argument on the floor. So
even a caller that *wanted* org policy applied had no way to ask for it.

This pins the seam. It does not pin that production uses it: nothing constructs
a fetcher yet, and nothing can, because ``GroupPolicyCache.materialize`` never
writes ``kind == "settings"`` entries to disk — so between one process and the
next there is nothing for a fetcher to read. Both of those live outside this
module; see the note on ``GroupPolicyTier``.

Before the fix ``load_settings(group_policy_fetcher=...)`` raised
``TypeError: load_settings() got an unexpected keyword argument``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ember_code.core.config.group_policy import GroupPolicyEntry, GroupPolicyPack
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
