"""Hooks run shell commands that no permission setting gates.

``--strict`` sets ``shell_execute: deny`` and the group tier can no longer
loosen it (``test_permission_ratchet.py``). That ratchet governs the
*agent's* tool calls. Hooks are a separate execution path:
``CommandHookHandler`` runs ``bash -c <command>`` and its ``run`` signature
is ``(hook, event, payload)`` — no permissions object is passed in, so
there is nothing for it to consult even in principle.

Verified rather than read: a ``PreToolUse`` hook was fired through the
handler and wrote a marker file to disk.

**This is pinned as intended behaviour, not fixed.** Hooks exist to run
commands; gating them on ``shell_execute`` would stop a group's
``pre-pr-review`` hook from running on exactly the machines whose
operators are most careful, and an org shipping automation that runs is
the feature. A developer's own hooks are their own choice.

What was wrong was the *claim*: ``--strict`` advertised "deny all
dangerous operations", which reads as covering shell execution on the
machine. It covers the agent. Group-supplied hooks are arbitrary local
code execution controlled by whoever administers the org, and no client
flag reduces that — which is a trust relationship to accept knowingly,
not a setting to discover afterwards.

So this file exists to make anyone who changes hook gating confront the
reasoning, and to fail if the ``--strict`` help text goes back to
promising more than it delivers.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from ember_code.cli.options import CliOptions
from ember_code.core.config.models import CliOverrides
from ember_code.core.hooks.handlers.command import CommandHookHandler
from ember_code.core.hooks.schemas import HookDefinition, HookPayload


class TestStrictDeniesTheAgentsShell:
    """The premise. If this breaks, the rest of the file is moot."""

    def test_strict_denies_shell_execute(self):
        overrides = CliOverrides.from_options(CliOptions(strict=True))

        assert overrides.permissions.shell_execute == "deny"


class TestAHookRunsAnyway:
    def test_the_handler_is_given_no_permissions_to_consult(self):
        """Asserted on the signature because it is the structural reason
        this cannot be a bug in the handler's logic: the information
        never arrives."""
        params = list(inspect.signature(CommandHookHandler.run).parameters)

        assert params == ["self", "hook", "event", "payload"]

    def test_it_executes_and_touches_the_filesystem(self, tmp_path: Path):
        marker = tmp_path / "HOOK-RAN"
        hook = HookDefinition(
            event="PreToolUse",
            type="command",
            command=f"echo executed > {marker}",
            matcher="Bash",
            timeout=5000,
        )
        payload = HookPayload(event="PreToolUse", tool_name="Bash", tool_input={"command": "ls"})

        asyncio.run(CommandHookHandler().run(hook, "PreToolUse", payload))

        assert marker.exists(), "the pinned behaviour is that it DOES run"
        assert marker.read_text().strip() == "executed"


class TestTheFlagDoesNotOverpromise:
    """The part that is actually a fix.

    A help string is the only description of ``--strict`` most people
    will ever read, so it is held to the same standard as the code.
    """

    def test_strict_help_does_not_claim_to_cover_everything(self):
        from ember_code.cli import cli

        strict = next(p for p in cli.params if p.name == "strict")
        help_text = (strict.help or "").lower()

        assert "deny all dangerous operations" not in help_text, (
            "'all' is not true while hooks run bash unconditionally — scope "
            "the claim to the agent's own tool use"
        )

    def test_strict_help_says_whose_actions_it_constrains(self):
        """Scoping it to the agent is the whole correction, so a future
        edit that drops the qualifier fails here."""
        from ember_code.cli import cli

        strict = next(p for p in cli.params if p.name == "strict")
        help_text = (strict.help or "").lower()

        assert "agent" in help_text, help_text
