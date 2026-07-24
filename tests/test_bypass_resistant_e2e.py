"""Row 9 of TESTING_PLAN.md: bypass-resistant scoped deny, pinned
end-to-end across every enforcement layer.

The unit suite covers each layer in isolation:

* ``test_permission_eval.py`` — evaluator pipeline (deny beats mode).
* ``test_handle_pause_evaluator.py`` — HITL pre-decide (deny short-
  circuits before reaching bypass auto-confirm).
* ``test_plan_mode.py::TestBypassSlashCommand`` — ``/bypass`` slash
  toggles the mode and broadcasts.
* ``test_hook_permission_decision.py`` — a PreToolUse ``allow``
  can't disarm the protected-paths / blocked-commands safety lists.

But the **full pipeline** — slash command flips mode → tool
dispatch hits the evaluator → deny rule blocks — wasn't pinned in
one place. Same for ``ToolEventHook`` running with mode=bypass
hitting protected_paths / blocked_commands: the safety net is
checked at step 2/3 before the evaluator's mode step, so it
should still fire, but no test asserts that exact combination.

That gap is what this file closes. Each test exercises the
narrowest possible slice that still crosses the seams that
matter.
"""

from __future__ import annotations

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
)
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.session.core import Session

# ── Slash command → mode flip → evaluator denies ─────────────


class TestSlashBypassThenDenyRule:
    """``/bypass on`` followed by a tool call that matches a deny
    rule must still block. The whole chain: slash dispatch →
    ``set_permission_mode("bypassPermissions")`` → next tool's
    ``evaluate`` call → DENY (because deny is step 2, mode is
    step 4)."""

    def _make_session(self, deny: list[str] | None = None):
        session = Session.__new__(Session)
        session.permission_evaluator = PermissionEvaluator.from_strings(
            mode="default",
            deny=deny or [],
        )
        from ember_code.core.session.broadcast import BroadcastBus

        session.broadcast_bus = BroadcastBus()
        return session

    @pytest.mark.asyncio
    async def test_bypass_then_deny_rule_still_blocks(self):
        session = self._make_session(deny=["Bash(rm *)"])
        handler = CommandHandler(session)

        # Step 1: user types /bypass on.
        await handler.handle("/bypass on")
        assert session.permission_evaluator.mode is PermissionMode.BYPASS_PERMISSIONS, (
            "preflight: /bypass on must flip the mode"
        )

        # Step 2: agent tries to rm. The evaluator's deny step
        # fires BEFORE the mode-auto-allow step, so the call
        # must DENY despite mode=bypassPermissions.
        decision = session.permission_evaluator.evaluate("Bash", {"command": "rm -rf /tmp/x"})
        assert decision is PermissionDecision.DENY, (
            "row-9 invariant broken: /bypass mode silently unlocked a "
            "scoped deny rule. This is the headline safety primitive — "
            "fix the evaluator pipeline ordering before shipping."
        )

    @pytest.mark.asyncio
    async def test_bypass_off_keeps_deny_blocking(self):
        # Round-trip: toggle bypass on, deny blocks; toggle off,
        # deny still blocks. The deny is mode-independent.
        session = self._make_session(deny=["Bash(rm *)"])
        handler = CommandHandler(session)

        await handler.handle("/bypass on")
        d1 = session.permission_evaluator.evaluate("Bash", {"command": "rm x"})
        await handler.handle("/bypass off")
        d2 = session.permission_evaluator.evaluate("Bash", {"command": "rm x"})

        assert d1 is PermissionDecision.DENY
        assert d2 is PermissionDecision.DENY, (
            "deny is mode-independent — toggling bypass off must "
            "not flip the deny decision in either direction."
        )

    @pytest.mark.asyncio
    async def test_bypass_allows_unrelated_calls(self):
        # The other half of the contract: bypass DOES auto-allow
        # everything that DOESN'T match a deny. Without this
        # assertion the test above could pass by trivially
        # denying everything.
        session = self._make_session(deny=["Bash(rm *)"])
        handler = CommandHandler(session)
        await handler.handle("/bypass on")

        decision = session.permission_evaluator.evaluate("Bash", {"command": "ls"})
        assert decision is PermissionDecision.ALLOW


# ── ToolEventHook safety lists + bypass mode ─────────────────


class TestSafetyListsBeatBypassMode:
    """The legacy safety lists (``protected_paths`` /
    ``blocked_commands``) live on ``ToolEventHook`` and fire at
    evaluator steps 2/3 — BEFORE the evaluator's mode step 4.
    Mode=bypass auto-allows everything at step 4, but it must
    not bypass the earlier safety lists.

    Existing tests pin the hook-``allow`` case
    (PreToolUse decision = allow vs safety list). This pins the
    mode-bypass case directly, which is the other half of the
    same invariant.
    """

    def _make_hook(
        self,
        protected_paths: list[str] | None = None,
        blocked_commands: list[str] | None = None,
    ) -> ToolEventHook:
        # No PreToolUse hooks — we're testing the safety list
        # interaction with mode=bypass, not the hook-allow
        # interaction.
        executor = HookExecutor({})
        ev = PermissionEvaluator.from_strings(mode="bypassPermissions")
        return ToolEventHook(
            executor=executor,
            session_id="s",
            protected_paths=protected_paths or [],
            blocked_commands=blocked_commands or [],
            permission_evaluator=ev,
        )

    @pytest.mark.asyncio
    async def test_bypass_mode_does_not_disarm_protected_paths(self):
        # Bypass auto-approves every unmatched call at step 4,
        # but ``.env`` is on the safety list at step 2 — must
        # still block. A regression that swaps step ordering
        # (mode before safety) would silently unlock credential
        # writes. This test catches it.
        hook = self._make_hook(protected_paths=[".env"])

        result = await hook(
            name="save_file",
            func=lambda file_path, content: "wrote",
            args={"file_path": ".env", "content": "SECRET=x"},
        )

        assert "protected path" in result.lower(), (
            "row-9 broken: mode=bypassPermissions silently allowed "
            "a write to a protected path. The safety list MUST run "
            "before the evaluator's mode step."
        )

    @pytest.mark.asyncio
    async def test_bypass_mode_does_not_disarm_blocked_commands(self):
        hook = self._make_hook(blocked_commands=["rm -rf /"])

        result = await hook(
            name="run_shell_command",
            func=lambda args: "ran",
            args={"args": ["rm", "-rf", "/"]},
        )

        assert "blocked pattern" in result.lower(), (
            "row-9 broken: mode=bypassPermissions silently allowed a "
            "blocked shell command. The safety list MUST run before "
            "the evaluator's mode step."
        )

    @pytest.mark.asyncio
    async def test_bypass_mode_allows_unrelated_writes(self):
        # Sanity: writes to UNLISTED paths still go through
        # under bypass. Without this the previous test could
        # pass by trivially blocking everything.
        hook = self._make_hook(protected_paths=[".env"])

        # Mock func so the hook's "call through" path returns a
        # success sentinel — we just need ``hook(...)`` not to
        # block.
        called: list[dict] = []

        def _func(file_path: str, content: str) -> str:
            called.append({"file_path": file_path, "content": content})
            return "wrote"

        result = await hook(
            name="save_file",
            func=_func,
            args={"file_path": "ok.py", "content": "x = 1"},
        )

        assert result == "wrote"
        assert called == [{"file_path": "ok.py", "content": "x = 1"}]


# ── Settings-tier deny survives bypass ───────────────────────


class TestSettingsTierDenySurvivesBypass:
    """The ``deny:`` list can come from any settings tier —
    user-global, project, managed. Once it's parsed into a
    ``PermissionEvaluator``, the source doesn't matter for the
    evaluation contract. But it's worth pinning that the
    factory entry point (``from_strings``, which is what the
    settings loader calls) honours the same invariant.

    The original bug shape: a user adds ``Bash(rm *)`` to
    ``~/.ember/settings.json``, types ``/bypass``, and rm goes
    through anyway because the deny was loaded into a different
    code path that didn't compose with the mode pipeline.
    """

    def test_deny_from_settings_blocks_under_bypass(self):
        # Simulates the settings-load path: settings.json
        # produces ``deny=["Bash(rm *)"]``, the loader calls
        # ``PermissionEvaluator.from_strings``, the user types
        # ``/bypass`` which flips the mode in place. The deny
        # must still bind.
        ev = PermissionEvaluator.from_strings(
            mode="bypassPermissions",
            deny=["Bash(rm *)"],
        )
        # Equivalent to a /bypass that started from default but
        # ended up with mode=bypass and the same deny list.
        assert ev.evaluate("Bash", {"command": "rm -rf /tmp/x"}) is PermissionDecision.DENY

    def test_multiple_deny_entries_all_bind_under_bypass(self):
        # Settings can carry a list of denies; all of them must
        # survive bypass, not just the first. Bug shape: a
        # loader iterates and only registers the first match,
        # the rest silently lose enforcement under bypass.
        ev = PermissionEvaluator.from_strings(
            mode="bypassPermissions",
            deny=["Bash(rm *)", "Edit(/etc/*)", "Bash(sudo *)"],
        )
        assert ev.evaluate("Bash", {"command": "rm -rf /"}) is PermissionDecision.DENY
        assert ev.evaluate("Edit", {"file_path": "/etc/passwd"}) is PermissionDecision.DENY
        assert ev.evaluate("Bash", {"command": "sudo apt update"}) is PermissionDecision.DENY
        # And a non-matching call still gets the bypass allow.
        assert ev.evaluate("Bash", {"command": "ls"}) is PermissionDecision.ALLOW

    def test_friendly_name_deny_survives_bypass_for_internal_tool(self):
        # Same invariant for friendly-name expansion: a settings
        # entry like ``Bash(rm *)`` is the CATALOG name but the
        # agent actually calls ``run_shell_command``. The
        # expansion + the bypass-mode interaction must both
        # honor the deny.
        ev = PermissionEvaluator.from_strings(
            mode="bypassPermissions",
            deny=["Bash(rm *)"],
        )
        assert (
            ev.evaluate("run_shell_command", {"command": "rm anything"}) is PermissionDecision.DENY
        )
