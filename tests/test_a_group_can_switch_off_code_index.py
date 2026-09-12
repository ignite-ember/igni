"""A group can turn the code index off, and a developer cannot turn it back on.

"Disable CodeIndex for this group, completely" needs two things to be
true, and only one of them is about the UI.

The first is that the switch means something. ``code_index.enabled``
is a kill switch rather than a preference: with it off the CodeIndex
tool is never offered to the agent, the plain ``main_agent`` prompt
replaces the CodeIndex-first variant, and the Neo4j sidecar is not
attached.

The second is that the group's copy wins. ``GroupPolicyTier`` sits
above the CLI tier precisely so "a user cannot escape org policy by
adding ``--auto-approve``", and below ``ManagedTier`` so a sysadmin
file still outranks an org. A policy a developer can switch back on in
their own ``config.yaml`` would not be a policy.

The frontend half — no menu entry, no completion, no help row, no
status chip — is tested in ``clients/web``; what is pinned here is
that the setting arrives and sticks.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

from ember_code.core.config.group_policy import GroupPolicyEntry, GroupPolicyPack
from ember_code.core.config.merge_plan import GroupPolicyTier, SettingsAccumulator


def pack(enabled: bool) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy.json",
                content=json.dumps({"code_index": {"enabled": enabled}}),
                content_type="json",
            )
        ],
    )


def apply(accumulator: SettingsAccumulator, enabled: bool) -> SettingsAccumulator:
    return GroupPolicyTier(fetcher=lambda: pack(enabled)).apply(accumulator)


class TestTheGroupCanTurnItOff:
    def test_a_settings_entry_reaches_the_setting(self):
        out = apply(SettingsAccumulator(), False)
        assert out.payload["code_index"]["enabled"] is False

    def test_it_overrides_a_developer_who_turned_it_on(self):
        """The whole point of a group policy.

        An explicit local ``enabled: true`` is exactly the case that
        must not survive — otherwise "disabled for this group" holds
        only for people who never looked at their own config.
        """
        local = SettingsAccumulator().merge({"code_index": {"enabled": True}})
        assert local.payload["code_index"]["enabled"] is True
        assert apply(local, False).payload["code_index"]["enabled"] is False

    def test_a_group_that_says_nothing_leaves_the_setting_alone(self):
        empty = GroupPolicyPack(
            group_id="g-1",
            group_name="Legal",
            fetched_at=datetime.now(timezone.utc),
            entries=[],
        )
        local = SettingsAccumulator().merge({"code_index": {"enabled": True}})
        out = GroupPolicyTier(fetcher=lambda: empty).apply(local)
        assert out.payload["code_index"]["enabled"] is True

    def test_no_group_at_all_leaves_the_setting_alone(self):
        local = SettingsAccumulator().merge({"code_index": {"enabled": True}})
        out = GroupPolicyTier(fetcher=lambda: None).apply(local)
        assert out.payload["code_index"]["enabled"] is True

    def test_the_switch_is_carried_on_the_status_update(self):
        """The frontend hides the menus off this field, so it has to
        exist and has to default to enabled — a backend that predates
        it is one where the index was on."""
        from ember_code.protocol.schemas.be_events import StatusUpdate

        assert StatusUpdate().code_index_enabled is True
        assert StatusUpdate(code_index_enabled=False).code_index_enabled is False


class TestSettingsUnrelatedToItSurvive:
    def test_other_settings_in_the_same_entry_still_merge(self):
        combined = GroupPolicyPack(
            group_id="g-1",
            group_name="Legal",
            fetched_at=datetime.now(timezone.utc),
            entries=[
                GroupPolicyEntry(
                    kind="settings",
                    entry_name="policy.json",
                    content=json.dumps(
                        {"code_index": {"enabled": False}, "knowledge": {"enabled": True}}
                    ),
                    content_type="json",
                )
            ],
        )
        out = GroupPolicyTier(fetcher=lambda: combined).apply(SettingsAccumulator())
        assert out.payload["code_index"]["enabled"] is False
        assert out.payload["knowledge"]["enabled"] is True
