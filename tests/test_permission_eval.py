"""Unit tests for the Claude Code-style permission evaluator.

Covers: ``Tool(pattern)`` rule parse + match, the 6-step pipeline
ordering (deny → ask → mode → allow → defer), bypass-resistant
scoped denies, ``plan`` blocking edit tools, ``acceptEdits``
auto-approving edit tools, ``dontAsk`` denying unmatched.
"""

from __future__ import annotations

from ember_code.core.config.permission_eval import (
    FILE_EDIT_TOOLS,
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
    PermissionRule,
)

# ── Rule parsing ──────────────────────────────────────────────────


def test_rule_parse_bare_name() -> None:
    r = PermissionRule.parse("Bash")
    assert r is not None
    assert r.tool == "Bash"
    assert r.pattern is None


def test_rule_parse_with_pattern() -> None:
    r = PermissionRule.parse("Bash(rm *)")
    assert r is not None
    assert r.tool == "Bash"
    assert r.pattern == "rm *"


def test_rule_parse_with_path_pattern() -> None:
    r = PermissionRule.parse("Read(./.env)")
    assert r is not None
    assert r.tool == "Read"
    assert r.pattern == "./.env"


def test_rule_parse_wildcard_tool() -> None:
    r = PermissionRule.parse("*")
    assert r is not None
    assert r.tool == "*"
    assert r.pattern is None


def test_rule_parse_empty_returns_none() -> None:
    assert PermissionRule.parse("") is None
    assert PermissionRule.parse("   ") is None


def test_rule_parse_malformed_returns_none() -> None:
    # No opening paren without parens / not a valid tool name.
    assert PermissionRule.parse("123_invalid") is None
    assert PermissionRule.parse("Bash(no-close") is None


# ── Rule matching ─────────────────────────────────────────────────


def test_bare_rule_matches_any_invocation() -> None:
    r = PermissionRule(tool="Bash", pattern=None)
    assert r.matches("Bash", {"command": "ls"}) is True
    assert r.matches("Bash", {}) is True


def test_bare_rule_does_not_match_other_tool() -> None:
    r = PermissionRule(tool="Bash", pattern=None)
    assert r.matches("Read", {"file_path": "x.py"}) is False


def test_pattern_matches_command() -> None:
    r = PermissionRule(tool="Bash", pattern="rm *")
    assert r.matches("Bash", {"command": "rm -rf build"}) is True
    assert r.matches("Bash", {"command": "ls"}) is False


def test_pattern_matches_file_path() -> None:
    r = PermissionRule(tool="Read", pattern="./.env*")
    assert r.matches("Read", {"file_path": "./.env"}) is True
    assert r.matches("Read", {"file_path": "./.env.local"}) is True
    assert r.matches("Read", {"file_path": "./README.md"}) is False


def test_pattern_matches_args_list() -> None:
    """Legacy shell tools that pass ``args=[...]`` instead of
    ``command="..."`` should still match the same patterns."""
    r = PermissionRule(tool="run_shell_command", pattern="rm *")
    assert r.matches("run_shell_command", {"args": ["rm", "-rf", "build"]}) is True


def test_wildcard_matches_anything() -> None:
    r = PermissionRule(tool="*", pattern=None)
    assert r.matches("Bash", {"command": "ls"}) is True
    assert r.matches("Read", {"file_path": "x"}) is True


# ── Pipeline ordering ─────────────────────────────────────────────


def test_deny_wins_over_allow() -> None:
    ev = PermissionEvaluator.from_strings(
        deny=["Bash(rm *)"],
        allow=["Bash"],
    )
    d = ev.evaluate("Bash", {"command": "rm -rf x"})
    assert d is PermissionDecision.DENY


def test_deny_wins_over_ask() -> None:
    ev = PermissionEvaluator.from_strings(deny=["Bash"], ask=["Bash(npm *)"])
    assert ev.evaluate("Bash", {"command": "npm test"}) is PermissionDecision.DENY


def test_ask_wins_over_allow() -> None:
    ev = PermissionEvaluator.from_strings(ask=["Bash(rm *)"], allow=["Bash"])
    assert ev.evaluate("Bash", {"command": "rm x"}) is PermissionDecision.ASK


def test_allow_returns_allow() -> None:
    ev = PermissionEvaluator.from_strings(allow=["Bash(npm test)"])
    assert ev.evaluate("Bash", {"command": "npm test"}) is PermissionDecision.ALLOW


def test_no_rule_default_mode_defers() -> None:
    ev = PermissionEvaluator.from_strings(mode="default")
    assert ev.evaluate("Bash", {"command": "ls"}) is PermissionDecision.DEFER


def test_no_rule_dont_ask_denies() -> None:
    """Headless mode: anything unmatched after the pipeline is a
    DENY (since we can't prompt), not a DEFER."""
    ev = PermissionEvaluator.from_strings(mode="dontAsk")
    assert ev.evaluate("Bash", {"command": "ls"}) is PermissionDecision.DENY


# ── Mode-specific behaviour ───────────────────────────────────────


def test_plan_mode_blocks_edit_tools() -> None:
    ev = PermissionEvaluator.from_strings(mode="plan")
    for tool in ("Edit", "Write", "save_file", "edit_file", "create_file"):
        assert ev.evaluate(tool, {"file_path": "x.py"}) is PermissionDecision.DENY, tool


def test_plan_mode_allows_read_tools() -> None:
    # Plan mode lets the agent investigate freely: ``Read`` (and
    # other tools in FILE_READ_TOOLS) auto-allow so the user isn't
    # prompted for every cat / grep / glob during planning.
    ev = PermissionEvaluator.from_strings(mode="plan")
    assert ev.evaluate("Read", {"file_path": "x.py"}) is PermissionDecision.ALLOW
    assert ev.evaluate("read_file", {"file_path": "x.py"}) is PermissionDecision.ALLOW
    assert ev.evaluate("Grep", {"pattern": "foo"}) is PermissionDecision.ALLOW
    assert ev.evaluate("Glob", {"pattern": "*.py"}) is PermissionDecision.ALLOW
    assert ev.evaluate("LS", {"path": "."}) is PermissionDecision.ALLOW


def test_plan_mode_allows_readonly_shell() -> None:
    # Shell commands that don't mutate the filesystem (cat, ls,
    # grep, find, head, …) are allowed under plan mode.
    ev = PermissionEvaluator.from_strings(mode="plan")
    for cmd in ("ls -la", "cat src/cli.py", "grep foo src", "find . -name '*.py'"):
        assert ev.evaluate("Bash", {"command": cmd}) is PermissionDecision.ALLOW, cmd
        assert ev.evaluate("run_shell_command", {"command": cmd}) is PermissionDecision.ALLOW, cmd


def test_plan_mode_blocks_mutating_shell() -> None:
    # Plan mode says "mutating shell commands are blocked" — sed -i,
    # > redirects, rm/mv/cp/etc must DENY even though they go through
    # Bash (which isn't in FILE_EDIT_TOOLS).
    ev = PermissionEvaluator.from_strings(mode="plan")
    mutating = [
        "sed -i '1s/^/# header\\n/' file.py",
        "echo hi > file.txt",
        "echo more >> file.txt",
        "rm -rf /tmp/x",
        "mv a b",
        "cp src dst",
        "mkdir new_dir",
        "touch newfile",
        "chmod 755 script.sh",
        "tee output.txt",
        "perl -i.bak -pe 's/x/y/' file",
    ]
    for cmd in mutating:
        assert ev.evaluate("Bash", {"command": cmd}) is PermissionDecision.DENY, (
            f"plan mode should DENY: {cmd!r}"
        )


def test_explain_deny_plan_mode_edit() -> None:
    # The reject note must tell the agent it's in plan mode and
    # point at exit_plan_mode — without this hint the model treats
    # the block as a hostile environment and asks the user to run
    # the command manually.
    from ember_code.core.config.permission_eval import explain_deny

    ev = PermissionEvaluator.from_strings(mode="plan")
    reason = explain_deny(ev, "edit_file", {"file_path": "x.py"})
    assert "plan mode" in reason
    assert "exit_plan_mode" in reason


def test_explain_deny_plan_mode_mutating_shell() -> None:
    from ember_code.core.config.permission_eval import explain_deny

    ev = PermissionEvaluator.from_strings(mode="plan")
    reason = explain_deny(ev, "run_shell_command", {"command": "sed -i '' '1s/^/x\\n/' f.py"})
    assert "plan mode" in reason
    assert "mutating shell" in reason or "shell" in reason
    assert "exit_plan_mode" in reason


def test_explain_deny_scoped_deny_rule() -> None:
    from ember_code.core.config.permission_eval import explain_deny

    ev = PermissionEvaluator.from_strings(mode="bypassPermissions", deny=["Bash(rm *)"])
    reason = explain_deny(ev, "Bash", {"command": "rm -rf /tmp/x"})
    assert "deny rule" in reason
    assert "Bash(rm *)" in reason


def test_plan_mode_stderr_merge_not_treated_as_write() -> None:
    # ``2>&1`` is a stderr-to-stdout merge, not a file write.
    # The mutation regex must not flag it.
    ev = PermissionEvaluator.from_strings(mode="plan")
    assert ev.evaluate("Bash", {"command": "ls -la 2>&1 | grep foo"}) is PermissionDecision.ALLOW


def test_accept_edits_mode_allows_edit_tools() -> None:
    ev = PermissionEvaluator.from_strings(mode="acceptEdits")
    for tool in FILE_EDIT_TOOLS:
        assert ev.evaluate(tool, {"file_path": "x.py"}) is PermissionDecision.ALLOW, tool


def test_accept_edits_mode_defers_non_edit_tools() -> None:
    ev = PermissionEvaluator.from_strings(mode="acceptEdits")
    assert ev.evaluate("Bash", {"command": "ls"}) is PermissionDecision.DEFER


def test_bypass_permissions_allows_unmatched() -> None:
    ev = PermissionEvaluator.from_strings(mode="bypassPermissions")
    assert ev.evaluate("Bash", {"command": "ls"}) is PermissionDecision.ALLOW
    assert ev.evaluate("Read", {"file_path": "x"}) is PermissionDecision.ALLOW


# ── The headline safety invariant ─────────────────────────────────


def test_scoped_deny_survives_bypass_permissions() -> None:
    """``Bash(rm *)`` must STILL block ``rm`` even when the mode
    is ``bypassPermissions``. This is the headline safety primitive
    copied from Claude Code."""
    ev = PermissionEvaluator.from_strings(
        mode="bypassPermissions",
        deny=["Bash(rm *)"],
    )
    assert ev.evaluate("Bash", {"command": "rm -rf x"}) is PermissionDecision.DENY
    # Non-matching invocations still get the bypass-mode auto-allow.
    assert ev.evaluate("Bash", {"command": "ls"}) is PermissionDecision.ALLOW


def test_scoped_deny_survives_accept_edits() -> None:
    """``acceptEdits`` auto-approves Edit, but ``Edit(/etc/*)``
    still blocks. Same precedence rule."""
    ev = PermissionEvaluator.from_strings(
        mode="acceptEdits",
        deny=["Edit(/etc/*)"],
    )
    assert ev.evaluate("Edit", {"file_path": "/etc/passwd"}) is PermissionDecision.DENY
    assert ev.evaluate("Edit", {"file_path": "src/a.py"}) is PermissionDecision.ALLOW


def test_plan_mode_deny_still_wins() -> None:
    """``plan`` blocks edit tools and auto-allows reads; an explicit
    deny on a read tool is still honoured (otherwise the deny list
    would be silently eclipsed by the plan-mode auto-allow)."""
    ev = PermissionEvaluator.from_strings(mode="plan", deny=["Read(./.env)"])
    assert ev.evaluate("Read", {"file_path": "./.env"}) is PermissionDecision.DENY
    # Other reads still get the plan-mode auto-allow.
    assert ev.evaluate("Read", {"file_path": "x.py"}) is PermissionDecision.ALLOW


# ── Mode enum smoke ──────────────────────────────────────────────


def test_permission_mode_enum_values() -> None:
    assert PermissionMode.DEFAULT.value == "default"
    assert PermissionMode.DONT_ASK.value == "dontAsk"
    assert PermissionMode.ACCEPT_EDITS.value == "acceptEdits"
    assert PermissionMode.BYPASS_PERMISSIONS.value == "bypassPermissions"
    assert PermissionMode.PLAN.value == "plan"
    # ``auto`` (TS-only) intentionally absent.
    assert "auto" not in {m.value for m in PermissionMode}


def test_from_strings_drops_malformed_rules() -> None:
    """Garbage entries in the settings file shouldn't bring down
    the whole pipeline — they get dropped silently (caller can
    cross-check counts if it cares)."""
    ev = PermissionEvaluator.from_strings(
        deny=["Bash(rm *)", "", "garbage(", "123_invalid"],
    )
    assert len(ev.deny) == 1
    assert ev.deny[0].pattern == "rm *"
