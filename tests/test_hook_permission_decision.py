"""Tests for the CC-compatible ``permissionDecision`` envelope.

A ``PreToolUse`` hook can return JSON with
``hookSpecificOutput.permissionDecision`` to take over routing
from the permission pipeline: ``allow`` runs the tool (bypassing
the evaluator); ``deny`` blocks + fires ``PermissionDenied``;
``ask`` fires ``PermissionRequest`` and treats-as-deny; ``defer``
(or omitting the field) falls through to the rest of the pipeline.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from typing import Any

import pytest

from ember_code.core.config.permission_eval import PermissionEvaluator
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.schemas import HookDefinition, HookResult
from ember_code.core.hooks.tool_hook import ToolEventHook


def _hook_script(payload_json: str) -> str:
    """Write a tiny shell script that emits ``payload_json`` on
    stdout (used to feed hookSpecificOutput JSON back to the
    executor). Returns the script path."""
    fd, path = tempfile.mkstemp(suffix=".sh", prefix="hookscript-")
    os.close(fd)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
        f.write(f"cat <<'JSON_EOF'\n{payload_json}\nJSON_EOF\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def _cleanup(*paths: str) -> None:
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                shutil.rmtree(p, ignore_errors=True)


# ── Schema field present ─────────────────────────────────────────


def test_hook_result_has_permission_decision_field() -> None:
    r = HookResult(should_continue=True, permission_decision="allow")
    assert r.permission_decision == "allow"


def test_hook_result_default_permission_decision_is_empty() -> None:
    r = HookResult(should_continue=True)
    assert r.permission_decision == ""


# ── Stdout JSON parsing ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_parses_hookSpecificOutput_permissionDecision() -> None:
    """The canonical CC shape: stdout JSON is
    ``{"hookSpecificOutput": {"permissionDecision": "allow"}}``."""
    body = json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})
    script = _hook_script(body)
    try:
        hook = HookDefinition(type="command", command=script)
        executor = HookExecutor({"PreToolUse": [hook]})
        result = await executor.execute("PreToolUse", payload={})
        assert result.permission_decision == "allow"
    finally:
        _cleanup(script)


@pytest.mark.asyncio
async def test_executor_accepts_top_level_permissionDecision_as_fallback() -> None:
    """For ergonomics, ``{"permissionDecision": "deny"}`` at top
    level also works (no need to nest in hookSpecificOutput for
    the common case)."""
    body = json.dumps({"permissionDecision": "deny", "systemMessage": "no"})
    script = _hook_script(body)
    try:
        hook = HookDefinition(type="command", command=script)
        executor = HookExecutor({"PreToolUse": [hook]})
        result = await executor.execute("PreToolUse", payload={})
        assert result.permission_decision == "deny"
        assert result.message == "no"
    finally:
        _cleanup(script)


# ── tool_hook routing on each decision ───────────────────────────


async def _run_with_hook_decision(
    decision: str,
    *,
    deny_rule: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    """Helper: build a tool hook whose PreToolUse returns
    ``permissionDecision: <decision>``, optionally with a deny
    rule in the evaluator. Returns (result, recorded_calls)."""
    body: dict[str, Any] = {"hookSpecificOutput": {"permissionDecision": decision}}
    if extra_payload:
        body.update(extra_payload)
    script = _hook_script(json.dumps(body))
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Recorder(HookExecutor):
        def __init__(self) -> None:
            super().__init__({"PreToolUse": [HookDefinition(type="command", command=script)]})

        def get_matching_hooks(self, event: str, target: str = ""):
            # Real hook list for PreToolUse so the script runs;
            # a stub list for the synthetic permission events so
            # ``_fire`` doesn't short-circuit and we can record
            # them through ``execute`` below.
            if event == "PreToolUse":
                return super().get_matching_hooks(event, target)
            return [HookDefinition(type="command", command=":")]

        async def execute(self, event: str, payload: dict[str, Any], target: str = ""):
            calls.append((event, payload))
            # Delegate to the real executor only for the
            # PreToolUse event so the hook script actually runs;
            # synthetic PermissionDenied / PermissionRequest etc.
            # just get recorded.
            if event == "PreToolUse":
                return await super().execute(event, payload, target)
            return HookResult(should_continue=True)

    executor = _Recorder()
    evaluator = PermissionEvaluator.from_strings(deny=[deny_rule]) if deny_rule else None
    hook = ToolEventHook(
        executor=executor,
        session_id="s",
        permission_evaluator=evaluator,
    )

    def fake_tool(file_path: str) -> str:
        return f"ran {file_path}"

    result = await hook(name="file_read", func=fake_tool, args={"file_path": "x.py"})
    _cleanup(script)
    return result, calls


@pytest.mark.asyncio
async def test_allow_decision_bypasses_evaluator_deny() -> None:
    """A hook returning ``allow`` short-circuits the evaluator,
    even one with a deny rule that would otherwise block."""
    result, calls = await _run_with_hook_decision("allow", deny_rule="file_read(x.py)")
    assert "ran x.py" in result
    # No PermissionDenied because the evaluator was skipped.
    assert not any(c[0] == "PermissionDenied" for c in calls)


@pytest.mark.asyncio
async def test_deny_decision_blocks_and_fires_permission_denied() -> None:
    result, calls = await _run_with_hook_decision("deny")
    assert "Blocked" in result or "denied" in result.lower()
    denied = [c for c in calls if c[0] == "PermissionDenied"]
    assert len(denied) == 1
    assert denied[0][1]["reason"] == "pre_tool_use_hook"


@pytest.mark.asyncio
async def test_ask_decision_fires_permission_request_and_executes() -> None:
    """A PreToolUse ``ask`` fires the ``PermissionRequest`` event
    (so plugins / logs see the ask) but falls through to execution.
    Rationale: Agno's ``requires_confirmation`` HITL has already
    prompted the user by the time this hook runs — blocking here
    would double-ask, which surfaced as "no canUseTool bridge is
    wired yet" and prevented users from running shell commands
    after upgrading from an older version.

    A real canUseTool RPC will flip this back to blocking-on-answer;
    update this test then.
    """
    result, calls = await _run_with_hook_decision("ask")
    assert "ran x.py" in result
    requests = [c for c in calls if c[0] == "PermissionRequest"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_defer_decision_falls_through_to_evaluator() -> None:
    """defer (or empty) → the rest of the pipeline runs. With a
    deny rule in the evaluator, the evaluator's DENY wins."""
    result, calls = await _run_with_hook_decision("defer", deny_rule="file_read(x.py)")
    assert "Blocked" in result
    denied = [c for c in calls if c[0] == "PermissionDenied"]
    assert len(denied) == 1
    # The deny came from the evaluator, not the hook.
    assert denied[0][1]["reason"] == "permission_evaluator"


@pytest.mark.asyncio
async def test_empty_decision_falls_through() -> None:
    """No permissionDecision in the hook output → existing
    pipeline runs (no behavior change for hooks that haven't
    opted in to the new envelope)."""
    body = json.dumps({"systemMessage": "fyi"})
    script = _hook_script(body)
    try:
        executor = HookExecutor({"PreToolUse": [HookDefinition(type="command", command=script)]})
        hook = ToolEventHook(executor=executor, session_id="s")
        result = await hook(
            name="file_read", func=lambda file_path: "ok", args={"file_path": "x.py"}
        )
        assert result == "ok"
    finally:
        _cleanup(script)


@pytest.mark.asyncio
async def test_legacy_should_continue_false_still_blocks() -> None:
    """Back-compat: a hook that exits with code 2 (block) still
    blocks without needing a permission_decision field."""
    fd, path = tempfile.mkstemp(suffix=".sh", prefix="hookblock-")
    os.close(fd)
    with open(path, "w") as f:
        f.write("#!/bin/sh\necho 'go away' >&2\nexit 2\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    try:
        executor = HookExecutor({"PreToolUse": [HookDefinition(type="command", command=path)]})
        hook = ToolEventHook(executor=executor, session_id="s")
        result = await hook(
            name="file_read", func=lambda file_path: "ok", args={"file_path": "x.py"}
        )
        assert "go away" in result or "Blocked" in result
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_allow_does_not_bypass_protected_paths() -> None:
    """SAFETY: a hook ``allow`` cannot disarm the legacy
    protected-paths list. That list is the equivalent of CC's
    bypass-resistant scoped denies — a hook saying ``allow``
    or a mode like ``bypassPermissions`` should never silently
    unlock ``.env`` writes. Tests this invariant directly."""
    body = json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})
    script = _hook_script(body)
    try:
        executor = HookExecutor({"PreToolUse": [HookDefinition(type="command", command=script)]})
        hook = ToolEventHook(
            executor=executor,
            session_id="s",
            protected_paths=[".env"],
        )
        result = await hook(
            name="save_file",
            func=lambda file_path, content: "written",
            args={"file_path": ".env", "content": "SECRET=1"},
        )
        assert "protected path" in result.lower()
    finally:
        _cleanup(script)


@pytest.mark.asyncio
async def test_allow_does_not_bypass_blocked_commands() -> None:
    """Same safety invariant for the shell-command list — a
    PreToolUse ``allow`` doesn't unlock ``rm -rf /``."""
    body = json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})
    script = _hook_script(body)
    try:
        executor = HookExecutor({"PreToolUse": [HookDefinition(type="command", command=script)]})
        hook = ToolEventHook(
            executor=executor,
            session_id="s",
            blocked_commands=["rm -rf /"],
        )
        result = await hook(
            name="run_shell_command",
            func=lambda args: "ran",
            args={"args": ["rm", "-rf", "/"]},
        )
        assert "blocked pattern" in result.lower()
    finally:
        _cleanup(script)
