"""Tests for queue_hook.py — message queue injection during agent execution."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ember_code.core.queue_hook import (
    USER_NOTE_HEADER,
    QueueInjectorHook,
    QueuePersisterHook,
    create_queue_hook,
)


class TestQueueInjectorHook:
    @pytest.mark.asyncio
    async def test_passes_through_func_result(self):
        hook = QueueInjectorHook(queue=[])
        result = await hook(name="test", func=lambda: "hello", args={})
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_passes_args_to_func(self):
        hook = QueueInjectorHook(queue=[])
        result = await hook(name="add", func=lambda x, y: x + y, args={"x": 1, "y": 2})
        assert result == 3

    @pytest.mark.asyncio
    async def test_no_func_returns_none(self):
        hook = QueueInjectorHook(queue=[])
        result = await hook(name="test")
        assert result is None

    @pytest.mark.asyncio
    async def test_appends_queued_messages_to_tool_result(self):
        queue = ["message 1", "message 2"]
        hook = QueueInjectorHook(queue=queue)
        result = await hook(name="tool", func=lambda: "tool output", args={})
        assert isinstance(result, str)
        assert result.startswith("tool output")
        assert USER_NOTE_HEADER in result
        assert "message 1" in result
        assert "message 2" in result
        assert queue == []

    @pytest.mark.asyncio
    async def test_no_modification_when_queue_empty(self):
        hook = QueueInjectorHook(queue=[])
        result = await hook(name="tool", func=lambda: "raw output", args={})
        assert result == "raw output"

    @pytest.mark.asyncio
    async def test_handles_none_result(self):
        # Some tools return None (e.g. notify-style side-effect tools).
        hook = QueueInjectorHook(queue=["heads up"])
        result = await hook(name="tool", func=lambda: None, args={})
        assert isinstance(result, str)
        assert "heads up" in result
        assert USER_NOTE_HEADER in result

    @pytest.mark.asyncio
    async def test_handles_non_string_result(self):
        hook = QueueInjectorHook(queue=["mid-run"])
        result = await hook(name="tool", func=lambda: {"k": "v"}, args={})
        assert isinstance(result, str)
        assert "mid-run" in result
        # Original payload preserved (repr).
        assert "{'k': 'v'}" in result

    @pytest.mark.asyncio
    async def test_calls_on_inject_callback(self):
        on_inject = MagicMock()
        hook = QueueInjectorHook(queue=["hello"], on_inject=on_inject)
        await hook(name="tool", func=lambda: None, args={})
        on_inject.assert_called_once_with("hello")

    @pytest.mark.asyncio
    async def test_calls_on_queue_changed_callback(self):
        on_changed = MagicMock()
        hook = QueueInjectorHook(queue=["msg"], on_queue_changed=on_changed)
        await hook(name="tool", func=lambda: None, args={})
        on_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_awaits_async_func(self):
        hook = QueueInjectorHook(queue=[])

        async def async_func():
            return "async ok"

        result = await hook(name="test", func=async_func, args={})
        assert result == "async ok"

    @pytest.mark.asyncio
    async def test_appends_after_async_func(self):
        hook = QueueInjectorHook(queue=["queued during async tool"])

        async def async_func():
            return "async result"

        result = await hook(name="test", func=async_func, args={})
        assert result.startswith("async result")
        assert "queued during async tool" in result

    def test_reset_is_idempotent(self):
        hook = QueueInjectorHook(queue=[])
        hook.reset()  # no-op now; just shouldn't raise
        hook.reset()

    @pytest.mark.asyncio
    async def test_tracks_injected_for_persistence(self):
        hook = QueueInjectorHook(queue=["a", "b"])
        await hook(name="t", func=lambda: "ok", args={})
        assert hook.injected_this_run == ["a", "b"]
        hook.clear_injected_this_run()
        assert hook.injected_this_run == []


class TestQueuePersisterHook:
    def test_appends_user_messages_to_run_output(self):
        injector = QueueInjectorHook(queue=[])
        # Simulate two messages drained during the run.
        injector._injected_this_run = ["first", "second"]
        persister = QueuePersisterHook(injector)

        run_output = SimpleNamespace(messages=[])
        persister(run_output=run_output)

        assert len(run_output.messages) == 2
        assert run_output.messages[0].role == "user"
        assert run_output.messages[0].content == "first"
        assert run_output.messages[1].content == "second"
        assert injector.injected_this_run == []

    def test_initialises_messages_when_none(self):
        injector = QueueInjectorHook(queue=[])
        injector._injected_this_run = ["x"]
        persister = QueuePersisterHook(injector)

        run_output = SimpleNamespace(messages=None)
        persister(run_output=run_output)

        assert run_output.messages is not None
        assert run_output.messages[0].content == "x"

    def test_no_injected_means_no_op(self):
        injector = QueueInjectorHook(queue=[])
        persister = QueuePersisterHook(injector)
        run_output = SimpleNamespace(messages=[])
        persister(run_output=run_output)
        assert run_output.messages == []

    def test_no_run_output_clears_state(self):
        injector = QueueInjectorHook(queue=[])
        injector._injected_this_run = ["a"]
        persister = QueuePersisterHook(injector)
        persister(run_output=None)
        # State cleared even when there's nowhere to write.
        assert injector.injected_this_run == []


class TestCreateQueueHook:
    def test_returns_injector_and_persister(self):
        injector, persister = create_queue_hook([])
        assert isinstance(injector, QueueInjectorHook)
        assert isinstance(persister, QueuePersisterHook)
        assert persister._injector is injector

    def test_passes_callbacks(self):
        on_inject = MagicMock()
        on_changed = MagicMock()
        injector, _ = create_queue_hook([], on_inject=on_inject, on_queue_changed=on_changed)
        assert injector._on_inject is on_inject
        assert injector._on_queue_changed is on_changed

    @pytest.mark.asyncio
    async def test_full_flow_tool_then_persist(self):
        """End-to-end: inject → drain via tool_hook → persist via post_hook."""
        queue = ["mid-run note"]
        injector, persister = create_queue_hook(queue)

        # 1. Tool executes; queue drains into the tool result.
        result = await injector(name="t", func=lambda: "tool result", args={})
        assert "mid-run note" in result
        assert injector.injected_this_run == ["mid-run note"]

        # 2. Run completes; post_hook persists the user message.
        run_output = SimpleNamespace(messages=[])
        persister(run_output=run_output)
        assert len(run_output.messages) == 1
        assert run_output.messages[0].role == "user"
        assert run_output.messages[0].content == "mid-run note"


# ── Integration with Agno's real machinery ────────────────────────────


class TestAgnoIntegration:
    """Verify our hooks plug into Agno's actual hook pipeline correctly.

    These tests use Agno's real ``filter_hook_args`` and ``RunOutput`` types
    so a regression in Agno's contract (renamed kwarg, type change) breaks
    these tests rather than silently dropping queued messages in production.
    """

    def test_persister_invoked_with_real_filter_hook_args(self):
        """``filter_hook_args`` must hand our persister a usable run_output."""
        from agno.utils.hooks import filter_hook_args

        injector, persister = create_queue_hook([])
        # Mirror exactly what aexecute_post_hooks builds in Agno's _hooks.py.
        all_args = {
            "run_output": SimpleNamespace(messages=[]),
            "agent": "fake-agent",
            "session": "fake-session",
            "user_id": "u1",
            "run_context": "fake-rc",
            "debug_mode": False,
            "metadata": None,
        }
        injector._injected_this_run = ["typed during run"]
        persister(**filter_hook_args(persister, all_args))

        run_output = all_args["run_output"]
        assert len(run_output.messages) == 1
        assert run_output.messages[0].content == "typed during run"

    def test_appends_to_real_runoutput(self):
        """The real Agno RunOutput accepts our Message append unchanged."""
        from agno.run.agent import RunOutput

        injector, persister = create_queue_hook([])
        injector._injected_this_run = ["real-runoutput note"]

        run_output = RunOutput(run_id="test-run-1", session_id="s1")
        # Agno may initialise messages later; persister must handle None.
        assert run_output.messages is None
        persister(run_output=run_output)
        assert run_output.messages is not None
        assert len(run_output.messages) == 1
        msg = run_output.messages[0]
        assert msg.role == "user"
        assert msg.content == "real-runoutput note"
        # ``add_to_agent_memory`` must be True so Agno's run-end filter
        # ([m for m in run_messages.messages if m.add_to_agent_memory])
        # keeps the message in the persisted history.
        assert getattr(msg, "add_to_agent_memory", False) is True

    def test_message_passes_session_skip_roles_filter(self):
        """User messages survive ``skip_roles=['system','tool']`` filtering."""
        from agno.run.agent import RunOutput

        injector, persister = create_queue_hook([])
        injector._injected_this_run = ["should appear in /sessions"]

        run_output = RunOutput(run_id="r2", session_id="s2")
        persister(run_output=run_output)

        # Replicate the filter ``AgentSession.get_messages`` applies for
        # the typical "show user-visible history" path.
        skip_roles = {"system", "tool"}
        visible = [m for m in (run_output.messages or []) if m.role not in skip_roles]
        assert len(visible) == 1
        assert visible[0].content == "should appear in /sessions"


# ── Live LLM end-to-end test ──────────────────────────────────────────


class TestRealAgnoRun:
    """End-to-end test against a real LLM.

    Configure via env vars (loaded from .env at the repo root if present):
        EMBER_TEST_LLM_API_KEY    — required; OpenAI-compatible API key.
                                    Test is skipped if not set.
        EMBER_TEST_LLM_BASE_URL   — optional; defaults to OpenAI's API
        EMBER_TEST_LLM_MODEL      — optional; defaults to gpt-4o-mini
    """

    @pytest.mark.asyncio
    async def test_queued_message_appears_in_run_output_after_real_run(self):
        """Pre-populate the queue, run a real Agno Agent that calls one tool,
        and verify the queued user message lands in run_output.messages."""
        api_key = os.getenv("EMBER_TEST_LLM_API_KEY")
        if not api_key:
            pytest.skip(
                "EMBER_TEST_LLM_API_KEY not set (add it to .env or export it to run live tests)"
            )

        from agno.agent import Agent
        from agno.models.openai.like import OpenAILike

        queued_text = "PINEAPPLE-MARKER-9821"
        queue = [queued_text]
        injector, persister = create_queue_hook(queue)

        widget_calls: list[bool] = []

        def get_widget() -> str:
            """Return the secret widget code. Call this when asked."""
            widget_calls.append(True)
            return "WIDGET_CODE_99"

        base_url = os.getenv("EMBER_TEST_LLM_BASE_URL") or "https://api.openai.com/v1"
        model_id = os.getenv("EMBER_TEST_LLM_MODEL") or "gpt-4o-mini"

        agent = Agent(
            model=OpenAILike(id=model_id, api_key=api_key, base_url=base_url),
            tools=[get_widget],
            tool_hooks=[injector],
            post_hooks=[persister],
            instructions=(
                "Always call the get_widget tool before answering. "
                "Then state the widget code in your reply."
            ),
        )

        run_output = await agent.arun("What is the secret widget code?")

        # 1. The model actually called the tool — without that, the
        #    tool_hook never fires and the queue would never drain.
        assert widget_calls, "Model didn't call get_widget; cannot validate hook flow"

        # 2. The queue was drained during the run.
        assert queue == [], "Queue should have been drained by the tool_hook"
        assert injector.injected_this_run == [], "Persister should have cleared this"

        # 3. The queued message survives as a real user-role message in the
        #    persisted run record — this is the contract the previous fix
        #    didn't satisfy (the message used to vanish into a tool result).
        assert run_output.messages is not None
        user_contents = [m.content for m in run_output.messages if m.role == "user"]
        assert any(queued_text in (c or "") for c in user_contents), (
            f"Queued message not persisted as user-role. Roles seen: "
            f"{[(m.role, (m.content or '')[:60]) for m in run_output.messages]}"
        )

        # 4. The user-visible history filter (skip_roles=['system','tool'])
        #    still surfaces the queued message — it's not buried in tool output.
        visible = [m for m in run_output.messages if m.role not in {"system", "tool"}]
        assert any(queued_text in (m.content or "") for m in visible)
