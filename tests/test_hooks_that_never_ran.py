"""Four ways a configured hook produced no effect and no evidence.

Every case here is a hook the user wrote, the loader accepted, the
hooks panel listed — and that then did nothing, silently. That
combination is the worst failure this subsystem has, because the
hook's whole purpose is to be the thing you trust to have run.

* A ``background: true`` hook at ``SessionEnd`` was created as a task
  and cancelled before its first line, because ``execute`` had no
  suspension point left after launching it.
* Those tasks were also held by nothing, so the loop was free to
  collect them mid-await.
* A ``command`` hook whose script is missing exits 127 and fails
  open — which is the design — but logged nothing at any level, so
  "my security hook is installed" and "my security hook is a typo"
  looked identical.
* A hook whose ``type`` no handler implements loads fine and is
  skipped at DEBUG.

Plus the one advisory channel that was computed and dropped: a
PostToolUse hook's ``systemMessage``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.hook_firer import HookFirer
from ember_code.core.hooks.permission_pipeline import ToolCallContext
from ember_code.core.hooks.schemas import HookDefinition
from ember_code.core.hooks.tool_call_invoker import ToolCallInvoker


async def _cancel_sweep() -> None:
    """The teardown ``cli._run_then_drain`` performs verbatim: cancel
    every task other than this one, wait briefly, move on."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=5.0)


class TestBackgroundHooksSurviveLongEnoughToRun:
    @pytest.mark.asyncio
    async def test_a_background_session_end_hook_actually_runs(self, tmp_path: Path):
        """The reported defect, end to end.

        Before the drain existed this marker was never written: with
        no foreground SessionEnd hook, ``_await_foreground`` returns
        without ever yielding, so the background task had not been
        scheduled once when the CLI's cancel sweep reached it.
        """
        marker = tmp_path / "ran"
        executor = HookExecutor(
            {
                HookEvent.SESSION_END.value: [
                    HookDefinition(
                        type="command",
                        background=True,
                        command=f"printf ran > {marker}",
                    )
                ]
            }
        )

        await executor.execute(HookEvent.SESSION_END.value, payload={"session_id": "s"})
        await _cancel_sweep()

        assert marker.exists(), (
            "a background SessionEnd hook did not run before the session's own "
            "teardown cancelled it — the hook is unusable for its main purpose "
            "(flush state on exit)"
        )

    @pytest.mark.asyncio
    async def test_the_executor_holds_a_reference_to_every_background_task(self):
        """asyncio references running tasks weakly; a discarded handle
        is collectable mid-execution. Non-terminal events still return
        immediately, so the reference is the only thing keeping the
        task alive."""
        executor = HookExecutor(
            {
                HookEvent.SESSION_START.value: [
                    HookDefinition(type="command", background=True, command="sleep 0.3")
                ]
            }
        )

        await executor.execute(HookEvent.SESSION_START.value, payload={})

        live = [t for t in executor._background if not t.done()]
        assert len(live) == 1, "the background task is referenced by nothing"

        await executor.drain_background(timeout=5.0)
        assert not [t for t in executor._background if not t.done()]

    @pytest.mark.asyncio
    async def test_a_hanging_background_hook_cannot_hold_shutdown_open(self, tmp_path: Path):
        """The drain is bounded: a hook that outlives its own timeout
        is abandoned, not waited on."""
        executor = HookExecutor(
            {
                HookEvent.SESSION_END.value: [
                    HookDefinition(
                        type="command",
                        background=True,
                        command="sleep 30",
                        timeout=200,  # ms
                    )
                ]
            }
        )

        loop = asyncio.get_running_loop()
        started = loop.time()
        await executor.execute(HookEvent.SESSION_END.value, payload={})
        elapsed = loop.time() - started

        assert elapsed < 5.0, f"SessionEnd blocked for {elapsed:.1f}s on one hook"
        await _cancel_sweep()


class TestAHookThatCouldNotRunSaysSo:
    @pytest.mark.asyncio
    async def test_a_missing_hook_script_is_logged_at_warning(self, caplog):
        """``bash -c`` exits 127 for a script that isn't there. The
        call proceeds (hooks fail open) — but the operator must be
        able to tell that from "the hook ran and approved"."""
        caplog.set_level(logging.WARNING)
        executor = HookExecutor(
            {
                HookEvent.PRE_TOOL_USE.value: [
                    HookDefinition(
                        type="command",
                        command=".igni/hooks/definitely-not-here.sh",
                        matcher="run_shell_command",
                    )
                ]
            }
        )

        result = await executor.execute(
            HookEvent.PRE_TOOL_USE.value, payload={}, target="run_shell_command"
        )

        assert result.should_continue is True  # fail open is the design
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a PreToolUse hook that never ran left no trace at any level"
        assert "definitely-not-here.sh" in warnings[0].getMessage()
        assert "127" in warnings[0].getMessage()

    def test_a_type_no_handler_implements_is_named_at_load(self, caplog):
        """``type`` stays permissive so a settings file with a typo
        still loads — but the skip was DEBUG-only, which made a
        misspelt type indistinguishable from a working one."""
        caplog.set_level(logging.WARNING)

        HookExecutor({HookEvent.PRE_TOOL_USE.value: [HookDefinition(type="agent", command="x")]})

        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("agent" in m and "PreToolUse" in m for m in messages), messages


class TestPostToolUseCanReachTheModel:
    @pytest.mark.asyncio
    async def test_a_post_hook_message_is_appended_to_a_string_result(self):
        """PostToolUse cannot block — the call already ran — so the
        annotated tool result is its only channel back. It was
        computed and discarded."""
        executor = HookExecutor(
            {
                HookEvent.POST_TOOL_USE.value: [
                    HookDefinition(type="prompt", text="reminder: run the linter")
                ]
            }
        )
        invoker = ToolCallInvoker(HookFirer(executor, session_id="s"))
        ctx = ToolCallContext(
            name="edit_file",
            func=lambda **_: "wrote 3 lines",
            args={"file_path": "a.py"},
            agent=None,
        )

        out = await invoker.run(ctx)

        assert "wrote 3 lines" in out
        assert "reminder: run the linter" in out

    @pytest.mark.asyncio
    async def test_a_non_string_result_is_not_coerced(self):
        """A tool returning structured data must survive an advisory
        hook untouched — appending text would corrupt it."""
        executor = HookExecutor(
            {HookEvent.POST_TOOL_USE.value: [HookDefinition(type="prompt", text="note")]}
        )
        invoker = ToolCallInvoker(HookFirer(executor, session_id="s"))
        payload: dict[str, Any] = {"rows": 2}
        ctx = ToolCallContext(name="query", func=lambda **_: payload, args={}, agent=None)

        assert await invoker.run(ctx) == payload

    @pytest.mark.asyncio
    async def test_no_post_hook_leaves_the_result_byte_identical(self):
        """The common path: nothing subscribed, nothing appended."""
        executor = HookExecutor({})
        invoker = ToolCallInvoker(HookFirer(executor, session_id="s"))
        ctx = ToolCallContext(name="file_read", func=lambda **_: "body", args={}, agent=None)

        assert await invoker.run(ctx) == "body"
