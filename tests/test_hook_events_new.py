"""Tests for the new hook events: PreCompact / PostCompact /
InstructionsLoaded. Focuses on the firing contract (events emitted
with expected payload) rather than re-testing the executor itself."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember_code.core.config.permission_eval import PermissionEvaluator
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.schemas import HookDefinition, HookResult
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.utils.rules_index import RulesIndex


@pytest.mark.asyncio
async def test_permission_denied_event_fires_on_deny_rule(tmp_path: Path) -> None:
    """A ``deny`` rule that matches → tool result is the deny
    message AND ``PermissionDenied`` hook fires."""
    executor = _RecordingExecutor()
    evaluator = PermissionEvaluator.from_strings(deny=["run_shell_command(rm *)"])
    hook = ToolEventHook(
        executor=executor,
        session_id="s",
        project_dir=tmp_path,
        permission_evaluator=evaluator,
    )

    def fake_tool(command: str) -> str:
        return f"ran {command}"

    result = await hook(
        name="run_shell_command",
        func=fake_tool,
        args={"command": "rm -rf build"},
    )
    assert "Blocked" in result
    denied = [c for c in executor.calls if c[0] == "PermissionDenied"]
    assert len(denied) == 1
    payload = denied[0][1]
    assert payload["tool_name"] == "run_shell_command"
    assert payload["reason"] == "permission_evaluator"


@pytest.mark.asyncio
async def test_permission_request_event_fires_on_ask_rule(tmp_path: Path) -> None:
    """Until the canUseTool bridge is wired, ``ask`` → block +
    fire ``PermissionRequest`` for observers to react to."""
    executor = _RecordingExecutor()
    evaluator = PermissionEvaluator.from_strings(ask=["run_shell_command(npm *)"])
    hook = ToolEventHook(
        executor=executor,
        session_id="s",
        project_dir=tmp_path,
        permission_evaluator=evaluator,
    )

    def fake_tool(command: str) -> str:
        return "ran"

    result = await hook(
        name="run_shell_command",
        func=fake_tool,
        args={"command": "npm install"},
    )
    assert "approval" in result.lower() or "Blocked" in result
    requests = [c for c in executor.calls if c[0] == "PermissionRequest"]
    assert len(requests) == 1
    assert requests[0][1]["tool_name"] == "run_shell_command"


@pytest.mark.asyncio
async def test_evaluator_allow_lets_tool_through(tmp_path: Path) -> None:
    """ALLOW path: tool actually runs, no PermissionDenied/Request."""
    executor = _RecordingExecutor()
    evaluator = PermissionEvaluator.from_strings(
        mode="bypassPermissions",  # auto-allows unmatched
    )
    hook = ToolEventHook(
        executor=executor,
        session_id="s",
        project_dir=tmp_path,
        permission_evaluator=evaluator,
    )

    def fake_tool(file_path: str) -> str:
        return f"read {file_path}"

    result = await hook(
        name="file_read",
        func=fake_tool,
        args={"file_path": str(tmp_path / "x.py")},
    )
    assert "read" in result
    assert not any(c[0] == "PermissionDenied" for c in executor.calls)
    assert not any(c[0] == "PermissionRequest" for c in executor.calls)


class TestNewEventsRegistered:
    def test_pre_compact_in_enum(self) -> None:
        assert HookEvent.PRE_COMPACT.value == "PreCompact"

    def test_post_compact_in_enum(self) -> None:
        assert HookEvent.POST_COMPACT.value == "PostCompact"

    def test_instructions_loaded_in_enum(self) -> None:
        assert HookEvent.INSTRUCTIONS_LOADED.value == "InstructionsLoaded"

    def test_task_created_in_enum(self) -> None:
        assert HookEvent.TASK_CREATED.value == "TaskCreated"

    def test_task_completed_in_enum(self) -> None:
        assert HookEvent.TASK_COMPLETED.value == "TaskCompleted"

    def test_stop_failure_in_enum(self) -> None:
        assert HookEvent.STOP_FAILURE.value == "StopFailure"

    def test_permission_request_in_enum(self) -> None:
        assert HookEvent.PERMISSION_REQUEST.value == "PermissionRequest"

    def test_permission_denied_in_enum(self) -> None:
        assert HookEvent.PERMISSION_DENIED.value == "PermissionDenied"

    def test_count_grew_to_18(self) -> None:
        # Original 10 + (PreCompact/PostCompact/InstructionsLoaded) +
        # (TaskCreated/TaskCompleted/StopFailure) +
        # (PermissionRequest/PermissionDenied) = 18.
        assert len(list(HookEvent)) == 18


class _RecordingExecutor(HookExecutor):
    """Captures every ``execute`` call without spawning subprocesses
    or HTTP requests — lets us assert payload shape directly."""

    def __init__(self) -> None:
        super().__init__({})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_matching_hooks(self, event: str, target: str = "") -> list[HookDefinition]:
        # Pretend a hook is registered so ``_fire`` actually calls
        # ``execute`` — otherwise it short-circuits.
        return [HookDefinition(type="command", command=":")]

    async def execute(
        self,
        event: str,
        payload: dict[str, Any],
        target: str = "",
    ) -> HookResult:
        self.calls.append((event, payload))
        return HookResult(should_continue=True)


@pytest.mark.asyncio
async def test_instructions_loaded_fires_with_rules_payload(tmp_path: Path) -> None:
    """When the agent touches a file under a directory with a
    rules file, ``ToolEventHook`` discovers the rules AND fires
    ``InstructionsLoaded`` with the file list + byte total."""
    # Seed: rules file in a subdir, plus a touched file inside it.
    rules_dir = tmp_path / "svc"
    rules_dir.mkdir()
    (rules_dir / "ember.md").write_text("SVC-RULES-CONTENT")
    touched = rules_dir / "main.py"
    touched.write_text("# stub")

    idx = RulesIndex(tmp_path)
    executor = _RecordingExecutor()
    hook = ToolEventHook(
        executor=executor,
        session_id="sess-test",
        rules_index=idx,
        project_dir=tmp_path,
    )

    # Stub tool that returns a string result — the file path the
    # rules index will walk from is in ``file_path``.
    def fake_tool(file_path: str) -> str:
        return f"read {file_path}"

    result = await hook(
        name="file_read",
        func=fake_tool,
        args={"file_path": str(touched)},
    )

    # The tool result is suffixed with the discovered-rules block.
    assert "SVC-RULES-CONTENT" in result
    assert "<discovered-rules" in result

    # InstructionsLoaded was fired with the rules payload.
    instructions_calls = [c for c in executor.calls if c[0] == HookEvent.INSTRUCTIONS_LOADED.value]
    assert len(instructions_calls) == 1
    payload = instructions_calls[0][1]
    assert payload["source"] == "rules_index"
    assert payload["session_id"] == "sess-test"
    assert payload["files"] == ["svc/ember.md"]
    # bytes count is the utf-8 byte length of the rules content
    assert payload["bytes"] == len(b"SVC-RULES-CONTENT")


@pytest.mark.asyncio
async def test_instructions_loaded_quiet_when_no_rules_discovered(tmp_path: Path) -> None:
    """No rules under the touched path → no InstructionsLoaded
    event (avoids spamming observers with empty payloads)."""
    touched = tmp_path / "plain.py"
    touched.write_text("# stub")

    idx = RulesIndex(tmp_path)
    executor = _RecordingExecutor()
    hook = ToolEventHook(
        executor=executor,
        session_id="sess",
        rules_index=idx,
        project_dir=tmp_path,
    )

    def fake_tool(file_path: str) -> str:
        return "ok"

    await hook(name="file_read", func=fake_tool, args={"file_path": str(touched)})

    instructions_calls = [c for c in executor.calls if c[0] == HookEvent.INSTRUCTIONS_LOADED.value]
    assert instructions_calls == []


@pytest.mark.asyncio
async def test_task_created_and_completed_fire_via_scheduler_wrappers() -> None:
    """The scheduler wrappers built in ``start_scheduler`` translate
    runner-level ``on_task_started`` / ``on_task_completed`` callbacks
    into ``TaskCreated`` / ``TaskCompleted`` hook events."""
    import asyncio as _asyncio

    executor = _RecordingExecutor()
    session_id = "test-session"

    # Replicate the wrapper shape from server.py:start_scheduler
    # without standing up a full SchedulerRunner.
    def _on_started(task_id: str, description: str) -> None:
        _asyncio.create_task(
            executor.execute(
                event=HookEvent.TASK_CREATED.value,
                payload={
                    "session_id": session_id,
                    "task_id": task_id,
                    "description": description,
                },
            )
        )

    def _on_completed(task_id: str, description: str, success: bool) -> None:
        _asyncio.create_task(
            executor.execute(
                event=HookEvent.TASK_COMPLETED.value,
                payload={
                    "session_id": session_id,
                    "task_id": task_id,
                    "description": description,
                    "status": "completed" if success else "error",
                },
            )
        )

    _on_started("task-123", "do thing")
    _on_completed("task-123", "do thing", True)
    _on_completed("task-456", "thing that fails", False)
    # Let the create_task coros drain.
    await _asyncio.sleep(0)
    await _asyncio.sleep(0)

    events = [(c[0], c[1].get("status")) for c in executor.calls]
    assert (HookEvent.TASK_CREATED.value, None) in events
    completes = [c for c in executor.calls if c[0] == HookEvent.TASK_COMPLETED.value]
    statuses = {c[1]["status"] for c in completes}
    assert statuses == {"completed", "error"}


@pytest.mark.asyncio
async def test_instructions_loaded_does_not_re_fire_for_same_rule(tmp_path: Path) -> None:
    """Second tool call into the same subtree → no new rules
    surface (RulesIndex dedup), so no new event fires either."""
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "ember.md").write_text("R")
    (tmp_path / "svc" / "a.py").write_text("")
    (tmp_path / "svc" / "b.py").write_text("")

    idx = RulesIndex(tmp_path)
    executor = _RecordingExecutor()
    hook = ToolEventHook(
        executor=executor,
        session_id="s",
        rules_index=idx,
        project_dir=tmp_path,
    )

    def fake_tool(file_path: str) -> str:
        return "ok"

    await hook(name="file_read", func=fake_tool, args={"file_path": str(tmp_path / "svc" / "a.py")})
    await hook(name="file_read", func=fake_tool, args={"file_path": str(tmp_path / "svc" / "b.py")})

    fires = [c for c in executor.calls if c[0] == HookEvent.INSTRUCTIONS_LOADED.value]
    assert len(fires) == 1, "second call shouldn't re-fire — RulesIndex already showed the rule"
