"""The documented per-category permission levels actually decide something.

``permissions.file_write``, ``shell_execute``, ``web_search`` and ``web_fetch``
are presented in SECURITY.md, CONFIGURATION.md and QUICKSTART.md as igni's
permission model. Nothing read them.

Their only consumer was ``PermissionGuard``, which was never constructed —
anywhere, in ``src`` or ``tests``. The live path is ``PermissionEvaluator``,
which reads ``mode`` / ``deny`` / ``ask`` / ``allow`` and never looked at the
categories. So ``shell_execute: "deny"`` in a config file did nothing, and
``--no-web`` denied no web.

Twenty-one tests covered the dead machinery, which is precisely why it looked
maintained. They are gone; this file replaces them by testing the categories
where they now take effect.

The subtle half is which levels are emitted. Only values that *differ from the
schema default* become rules. Emitting defaults would have handed ``--strict``
an explicit ``allow`` for ``web_search`` — it defaults to ``"allow"`` — and
loosened a safety flag as a side effect of making config work. That is not
hypothetical: the first version did exactly that, and the probe caught it.
"""

from __future__ import annotations

import pytest

from ember_code.core.config.permission_eval import PermissionEvaluator
from ember_code.core.config.schemas.permissions import PermissionsConfig


def _decide(perms: PermissionsConfig, tool: str, args: dict) -> str:
    """Mirror what ``ToolHookFactory.permission_evaluator`` builds."""
    cat_deny, cat_ask, cat_allow = perms.category_rules()
    evaluator = PermissionEvaluator.from_strings(
        mode=perms.mode,
        deny=[*perms.deny, *cat_deny],
        ask=[*perms.ask, *cat_ask],
        allow=[*perms.allow, *cat_allow],
    )
    return evaluator.evaluate(tool, args).name


class TestALevelThatDiffersFromTheDefaultTakesEffect:
    def test_denying_writes_denies_the_write_tools(self):
        perms = PermissionsConfig(file_write="deny")

        assert _decide(perms, "edit_file", {"file_path": "a.py"}) == "DENY"
        assert _decide(perms, "save_file", {"file_name": "a.py"}) == "DENY"

    def test_denying_the_shell_denies_the_shell(self):
        perms = PermissionsConfig(shell_execute="deny")

        assert _decide(perms, "run_shell_command", {"command": "ls"}) == "DENY"

    def test_denying_web_denies_both_web_tools(self):
        """``--no-web`` sets these, and denied nothing until now."""
        perms = PermissionsConfig(web_search="deny", web_fetch="deny")

        assert _decide(perms, "web_search", {"query": "x"}) == "DENY"
        assert _decide(perms, "fetch_url", {"url": "http://x"}) == "DENY"


class TestDefaultsStayOutOfTheWay:
    """A default install must behave exactly as it did before the wiring."""

    def test_a_default_config_emits_no_rules(self):
        assert PermissionsConfig().category_rules() == ([], [], [])

    def test_strict_does_not_gain_web_access(self):
        """The regression this design exists to avoid.

        ``web_search`` defaults to ``"allow"``. Emitting defaults would give
        ``dontAsk`` an explicit allow rule for the web — turning a flag that
        denies everything unmatched into one that quietly permits outbound
        search. Caught by measuring, not by review.
        """
        perms = PermissionsConfig(mode="dontAsk", file_write="deny", shell_execute="deny")

        assert _decide(perms, "web_search", {"query": "x"}) == "DENY"


class TestACategoryDenyIsAFloor:
    def test_a_specific_allow_does_not_reopen_a_denied_category(self):
        """Deny wins, and list order does not change that.

        I expected the opposite when wiring this — that appending the category
        rules *behind* the explicit lists would let ``allow: ["Bash(git
        status)"]`` carve an exception out of ``shell_execute: "deny"``. It does
        not: the evaluator runs deny before mode, ask and allow, and its own
        docstring calls deny "the only always-wins rule (the safety floor)".

        Worth pinning rather than filing as a wrinkle. Setting a category to
        ``deny`` is a floor, not a default, so a user wanting most of the shell
        closed and one command open has to express that as ``deny`` rules for
        what they want closed — not a blanket deny plus exceptions.
        """
        perms = PermissionsConfig(shell_execute="deny", allow=["Bash(git status)"])

        assert _decide(perms, "run_shell_command", {"command": "git status"}) == "DENY"
        assert _decide(perms, "run_shell_command", {"command": "rm -rf /"}) == "DENY"


@pytest.mark.parametrize("category", sorted(PermissionsConfig.CATEGORY_FUNCTIONS))
def test_every_mapped_category_is_a_real_field(category: str):
    """A mapping keyed on a field that no longer exists would silently stop
    governing its tools — the failure the whole file is about."""
    assert category in PermissionsConfig.model_fields
