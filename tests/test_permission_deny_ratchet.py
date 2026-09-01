"""A group can tighten a permission, but not loosen one you denied.

``GroupPolicyTier`` sits above ``CliTier`` on purpose, and the stated
reason is one-directional: *"the whole point is that a user can't
override an org policy by adding ``--auto-approve`` on the command
line."*

The implementation was bidirectional, which is not the same claim. A
group ``settings`` entry could turn a developer's ``--strict`` —
``shell_execute: deny``, ``file_write: deny`` — into ``allow``, and
nothing said so. Verified before the change. A safety flag that silently
does nothing is worse than no flag, because the developer believes they
have a brake.

So a permission the CLI set to ``deny`` stays denied, and both directions
of the original intent survive: a group still tightens freely, and
``--auto-approve`` still cannot get past a group's ``deny``. Only the
loosening of an explicit local deny does not.

This is a ratchet on ``permissions`` denies specifically. Everything else
a group sets still wins over the CLI.
"""

from __future__ import annotations

import json
import logging

from ember_code.cli.options import CliOptions
from ember_code.core.config.accumulator import SettingsAccumulator
from ember_code.core.config.group_policy import GroupPolicyEntry, GroupPolicyPack
from ember_code.core.config.merge_plan import CliTier, GroupPolicyTier
from ember_code.core.config.models import CliOverrides

BASELINE = {
    "permissions": {
        "mode": "ask",
        "shell_execute": "ask",
        "file_write": "ask",
        "web_search": "ask",
    }
}


def _pack(payload: dict) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g",
        group_name="G",
        fetched_at=0.0,
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="s",
                content=json.dumps(payload),
                content_type="json",
                enabled=True,
            )
        ],
    )


def _resolve(options: CliOptions, group_payload: dict):
    """Run the CLI tier then the group tier, as ``default()`` wires them."""
    accumulator = SettingsAccumulator.from_defaults(BASELINE)
    cli_tier = CliTier(CliOverrides.from_options(options))
    accumulator = cli_tier.apply(accumulator)
    group_tier = GroupPolicyTier(fetcher=lambda: _pack(group_payload), cli_tier=cli_tier)
    accumulator = group_tier.apply(accumulator)
    return accumulator.payload["permissions"], group_tier


class TestTheRatchetHolds:
    def test_a_group_cannot_undo_strict(self):
        """The case that was broken: ``--strict`` denies shell execution
        and file writes, and a group said allow."""
        permissions, _ = _resolve(
            CliOptions(strict=True),
            {"permissions": {"shell_execute": "allow", "file_write": "allow"}},
        )

        assert permissions["shell_execute"] == "deny"
        assert permissions["file_write"] == "deny"

    def test_a_group_cannot_undo_read_only_either(self):
        """``--read-only`` is the same shape, and a fix for one flag that
        misses the other is not a fix."""
        permissions, _ = _resolve(
            CliOptions(read_only=True),
            {"permissions": {"file_write": "allow", "shell_execute": "allow"}},
        )

        assert permissions["file_write"] == "deny"
        assert permissions["shell_execute"] == "deny"

    def test_the_refusal_is_recorded(self):
        _, group_tier = _resolve(
            CliOptions(strict=True),
            {"permissions": {"shell_execute": "allow"}},
        )

        assert group_tier.refused_loosening == {"shell_execute": "allow"}

    def test_the_developer_is_told(self):
        """A policy that quietly does not apply is its own surprise — the
        admin believes they set something and nothing disagrees.

        Captured with a local handler and ``disabled`` cleared, not
        through ``caplog``. Something in this test session sets
        ``logger.disabled`` on module loggers and pytest's capture handler
        is removed from the root logger, so a caplog version passes in
        this file and fails in the suite — which is exactly what it did.
        ``tests/test_http_retry.py`` documents the pair at length,
        including that the cause of the flag is unidentified.
        """
        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        module_logger = logging.getLogger("ember_code.core.config.merge_plan")
        handler = _Collect()
        previous_level = module_logger.level
        previously_disabled = module_logger.disabled
        module_logger.disabled = False
        module_logger.addHandler(handler)
        module_logger.setLevel(logging.WARNING)
        try:
            _resolve(CliOptions(strict=True), {"permissions": {"shell_execute": "allow"}})
        finally:
            module_logger.removeHandler(handler)
            module_logger.setLevel(previous_level)
            module_logger.disabled = previously_disabled

        messages = " ".join(record.getMessage() for record in records)
        assert "shell_execute" in messages
        assert "the local deny stands" in messages


class TestBothDirectionsOfTheOriginalIntentSurvive:
    def test_a_group_can_still_tighten(self):
        """The reason the tier is above the CLI at all."""
        permissions, group_tier = _resolve(CliOptions(), {"permissions": {"shell_execute": "deny"}})

        assert permissions["shell_execute"] == "deny"
        assert group_tier.refused_loosening == {}

    def test_auto_approve_still_cannot_escape_a_group_deny(self):
        """*"A user can't override an org policy by adding
        ``--auto-approve``"* — the sentence the tier ordering exists for.
        """
        permissions, _ = _resolve(
            CliOptions(auto_approve=True), {"permissions": {"shell_execute": "deny"}}
        )

        assert permissions["shell_execute"] == "deny"

    def test_a_group_still_wins_on_everything_that_is_not_a_deny(self):
        """The ratchet is narrow on purpose: it protects local denies, not
        local preferences."""
        permissions, _ = _resolve(
            CliOptions(strict=True),
            {"permissions": {"web_search": "allow", "mode": "dontAsk"}},
        )

        assert permissions["web_search"] == "allow"
        assert permissions["mode"] == "dontAsk"

    def test_agreement_is_not_a_refusal(self):
        """Both saying deny is not the group being overruled, and must not
        produce a warning that implies it was."""
        permissions, group_tier = _resolve(
            CliOptions(strict=True), {"permissions": {"shell_execute": "deny"}}
        )

        assert permissions["shell_execute"] == "deny"
        assert group_tier.refused_loosening == {}


class TestNothingElseChanged:
    def test_a_group_with_no_permissions_block_is_untouched(self):
        permissions, group_tier = _resolve(
            CliOptions(strict=True), {"models": {"default": "some-model"}}
        )

        assert permissions["shell_execute"] == "deny"
        assert group_tier.refused_loosening == {}

    def test_no_group_at_all(self):
        accumulator = SettingsAccumulator.from_defaults(BASELINE)
        cli_tier = CliTier(CliOverrides.from_options(CliOptions(strict=True)))
        accumulator = cli_tier.apply(accumulator)

        group_tier = GroupPolicyTier(fetcher=lambda: None, cli_tier=cli_tier)
        result = group_tier.apply(accumulator)

        assert result.payload["permissions"]["shell_execute"] == "deny"

    def test_the_tier_works_without_a_cli_tier(self):
        """``cli_tier`` is optional, because tests and one-off migrations
        construct this tier directly. With none, there is nothing to
        ratchet against and the group simply wins."""
        accumulator = SettingsAccumulator.from_defaults(BASELINE)
        group_tier = GroupPolicyTier(
            fetcher=lambda: _pack({"permissions": {"shell_execute": "allow"}})
        )

        result = group_tier.apply(accumulator)

        assert result.payload["permissions"]["shell_execute"] == "allow"


class TestTheManagedTierStillOutranksEverything:
    def test_a_sysadmin_file_can_still_loosen(self):
        """The ratchet is between the group and the CLI. A sysadmin with
        filesystem access on the machine is a different authority, and
        deliberately above both — otherwise a local flag could veto the
        person who administers the host.
        """
        import inspect

        from ember_code.core.config import merge_plan

        source = inspect.getsource(merge_plan.SettingsMergePlan.default)
        managed_at = source.index("ManagedTier(")
        group_at = source.index("GroupPolicyTier(")

        assert group_at < managed_at, "managed policy must still run last"
