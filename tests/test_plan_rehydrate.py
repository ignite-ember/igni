"""``plan_store`` rehydrates from persisted ``exit_plan_mode`` tool calls.

The store lives in memory; on BE restart it's empty even when the
previous session clearly produced a plan. ``BackendServer.startup``
runs a one-shot rehydration that scans the resumed Agno session for
the most recent ``exit_plan_mode`` invocation and re-populates the
store from its arguments — so the FE's ``get_latest_plan`` RPC
returns the same PlanCard the user saw before close.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock  # noqa: F401  (used in tests)

from ember_code.backend.server import BackendServer
from ember_code.core.tools.plan import PlanStore


def _assistant_msg_with_tool_call(name: str, args: dict | str) -> SimpleNamespace:
    args_payload = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": name, "arguments": args_payload},
            }
        ],
    )


def _run(messages: list) -> SimpleNamespace:
    return SimpleNamespace(run_id="run_x", messages=messages)


def _make_backend_with_runs(runs: list) -> tuple[BackendServer, PlanStore]:
    server = BackendServer.__new__(BackendServer)
    server._session = MagicMock()
    server._session.plan_store = PlanStore()
    server._session.todo_store = MagicMock()
    server._session.todo_store.set = MagicMock()
    server._session.session_id = "sess-1"
    server._session.user_id = "u"
    agno_session = SimpleNamespace(runs=runs)
    server._session.main_team.aget_session = AsyncMock(return_value=agno_session)
    return server, server._session.plan_store


class TestPlanRehydrate:
    async def test_no_session_leaves_store_empty(self) -> None:
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.plan_store = PlanStore()
        server._session.session_id = "sess"
        server._session.user_id = "u"
        server._session.main_team.aget_session = AsyncMock(return_value=None)

        await server._rehydrate_plan_store()

        assert server._session.plan_store.latest == ""

    async def test_already_populated_store_is_left_alone(self) -> None:
        server, store = _make_backend_with_runs([])
        store.set_plan("Live plan from current session.")

        await server._rehydrate_plan_store()

        assert store.latest == "Live plan from current session."
        # And we never touched the agno session (early exit).
        server._session.main_team.aget_session.assert_not_called()

    async def test_finds_plan_in_last_run(self) -> None:
        runs = [
            _run(
                [
                    SimpleNamespace(role="user", content="please plan it"),
                    _assistant_msg_with_tool_call(
                        "exit_plan_mode",
                        {"plan": "Step 1: do thing.\nStep 2: do other thing."},
                    ),
                ]
            )
        ]
        server, store = _make_backend_with_runs(runs)

        await server._rehydrate_plan_store()

        assert store.latest == "Step 1: do thing.\nStep 2: do other thing."

    async def test_picks_most_recent_when_multiple_plans(self) -> None:
        # Two runs each with a plan; the LATER run's plan wins.
        runs = [
            _run(
                [
                    _assistant_msg_with_tool_call("exit_plan_mode", {"plan": "Old plan."}),
                ]
            ),
            _run(
                [
                    _assistant_msg_with_tool_call("exit_plan_mode", {"plan": "Newer plan."}),
                ]
            ),
        ]
        server, store = _make_backend_with_runs(runs)

        await server._rehydrate_plan_store()

        assert store.latest == "Newer plan."

    async def test_skips_other_tool_calls(self) -> None:
        runs = [
            _run(
                [
                    _assistant_msg_with_tool_call("run_shell_command", {"command": "ls"}),
                    _assistant_msg_with_tool_call("exit_plan_mode", {"plan": "The real plan."}),
                    _assistant_msg_with_tool_call(
                        "edit_file", {"file_path": "a.py", "old_string": "x", "new_string": "y"}
                    ),
                ]
            )
        ]
        server, store = _make_backend_with_runs(runs)

        await server._rehydrate_plan_store()

        assert store.latest == "The real plan."

    async def test_empty_plan_arg_is_ignored(self) -> None:
        runs = [
            _run(
                [
                    _assistant_msg_with_tool_call("exit_plan_mode", {"plan": ""}),
                    _assistant_msg_with_tool_call("exit_plan_mode", {"plan": "Good plan."}),
                ]
            )
        ]
        server, store = _make_backend_with_runs(runs)

        await server._rehydrate_plan_store()

        assert store.latest == "Good plan."

    async def test_malformed_json_arguments_skip(self) -> None:
        runs = [
            _run(
                [
                    _assistant_msg_with_tool_call("exit_plan_mode", "not json {"),
                    _assistant_msg_with_tool_call("exit_plan_mode", {"plan": "Recovered plan."}),
                ]
            )
        ]
        server, store = _make_backend_with_runs(runs)

        await server._rehydrate_plan_store()

        assert store.latest == "Recovered plan."

    async def test_aget_session_exception_swallowed(self) -> None:
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.plan_store = PlanStore()
        server._session.session_id = "sess"
        server._session.user_id = "u"
        server._session.main_team.aget_session = AsyncMock(side_effect=RuntimeError("db gone"))

        # Must not raise.
        await server._rehydrate_plan_store()

        assert server._session.plan_store.latest == ""

    def test_infer_plan_state_empty_plan(self) -> None:
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.permission_evaluator = SimpleNamespace(
            mode=SimpleNamespace(value="default")
        )
        assert server._infer_plan_state("") == ""

    def test_infer_plan_state_pending_when_still_in_plan_mode(self) -> None:
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.permission_evaluator = SimpleNamespace(mode=SimpleNamespace(value="plan"))
        assert server._infer_plan_state("some plan text") == "pending"

    def test_infer_plan_state_approved_when_mode_default(self) -> None:
        # User clicked Approve → /plan off fired → mode flipped to
        # default. On restore we treat that as approved.
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.permission_evaluator = SimpleNamespace(
            mode=SimpleNamespace(value="default")
        )
        assert server._infer_plan_state("some plan text") == "approved"

    def test_infer_plan_state_no_evaluator_defaults_to_approved(self) -> None:
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.permission_evaluator = None
        assert server._infer_plan_state("some plan text") == "approved"

    async def test_get_chat_history_emits_plan_turn_inline(self) -> None:
        # The agent submits a plan → assistant message has tool_calls,
        # tool message has the result. ``get_chat_history`` must
        # replace the tool result with a ``role: "plan"`` turn so the
        # FE PlanCard lands at the exit_plan_mode position, NOT at
        # the very end of the chat list.
        from ember_code.backend.server import BackendServer

        assistant_msg = SimpleNamespace(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_plan_1",
                    "type": "function",
                    "function": {
                        "name": "exit_plan_mode",
                        "arguments": json.dumps(
                            {
                                "plan": "Step 1.\nStep 2.",
                                "tasks": [{"content": "do thing", "status": "pending"}],
                            }
                        ),
                    },
                }
            ],
            reasoning_content=None,
            from_history=False,
            created_at=10,
        )
        tool_msg = SimpleNamespace(
            role="tool",
            content="Plan submitted. Stop here…",
            tool_name="exit_plan_mode",
            tool_args=None,
            tool_call_id="call_plan_1",
            tool_call_error=False,
            from_history=False,
            created_at=11,
        )
        user_msg = SimpleNamespace(
            role="user",
            content="please plan it",
            tool_calls=None,
            reasoning_content=None,
            from_history=False,
            created_at=9,
        )
        run = SimpleNamespace(
            run_id="r1",
            parent_run_id=None,
            messages=[user_msg, assistant_msg, tool_msg],
            metrics=None,
        )
        agno_session = SimpleNamespace(runs=[run])
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.user_id = "u"
        server._session.main_team.aget_session = AsyncMock(return_value=agno_session)
        server._session.permission_evaluator = SimpleNamespace(
            mode=SimpleNamespace(value="default")
        )

        history = await server.get_chat_history("sess")

        # Should contain a plan turn between the user and stats.
        plan_turns = [t for t in history if t.get("role") == "plan"]
        assert len(plan_turns) == 1
        assert plan_turns[0]["plan"] == "Step 1.\nStep 2."
        assert plan_turns[0]["tasks"] == [{"content": "do thing", "status": "pending"}]
        assert plan_turns[0]["state"] == "approved"
        # And NO regular tool turn for the same exit_plan_mode call.
        tool_turns = [t for t in history if t.get("role") == "tool"]
        assert not any(t.get("tool_name") == "exit_plan_mode" for t in tool_turns), (
            f"tool turn for exit_plan_mode leaked through: {tool_turns}"
        )

    def test_split_assistant_no_think_tags(self) -> None:
        from ember_code.backend.server import _split_assistant_content_for_restore

        assert _split_assistant_content_for_restore("Hello world.") == [
            ("assistant", "Hello world.")
        ]

    def test_split_assistant_only_whitespace(self) -> None:
        from ember_code.backend.server import _split_assistant_content_for_restore

        assert _split_assistant_content_for_restore("   ") == []

    def test_split_assistant_inline_think_block(self) -> None:
        from ember_code.backend.server import _split_assistant_content_for_restore

        parts = _split_assistant_content_for_restore("<think>reasoning here</think>Final answer.")
        assert parts == [("thinking", "reasoning here"), ("assistant", "Final answer.")]

    def test_split_assistant_text_then_think_then_text(self) -> None:
        from ember_code.backend.server import _split_assistant_content_for_restore

        parts = _split_assistant_content_for_restore("Starting now. <think>checking</think>Done.")
        assert parts == [
            ("assistant", "Starting now."),
            ("thinking", "checking"),
            ("assistant", "Done."),
        ]

    def test_split_assistant_only_think_block(self) -> None:
        from ember_code.backend.server import _split_assistant_content_for_restore

        # A cancelled run can leave nothing but a think block — no
        # assistant text should be emitted.
        assert _split_assistant_content_for_restore("<think>just thoughts</think>") == [
            ("thinking", "just thoughts")
        ]

    def test_split_assistant_unclosed_trailing_think(self) -> None:
        from ember_code.backend.server import _split_assistant_content_for_restore

        # Cancelled mid-thought — extract up to end-of-content.
        parts = _split_assistant_content_for_restore("Partial. <think>was still")
        assert parts == [("assistant", "Partial."), ("thinking", "was still")]

    async def test_get_chat_history_extracts_inline_think_tags(self) -> None:
        from ember_code.backend.server import BackendServer

        assistant_msg = SimpleNamespace(
            role="assistant",
            content="<think>let me reason</think>The answer is 42.",
            tool_calls=None,
            reasoning_content=None,
            from_history=False,
            created_at=10,
        )
        run = SimpleNamespace(
            run_id="r1",
            parent_run_id=None,
            messages=[assistant_msg],
            metrics=None,
        )
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.user_id = "u"
        server._session.main_team.aget_session = AsyncMock(return_value=SimpleNamespace(runs=[run]))
        server._session.permission_evaluator = SimpleNamespace(
            mode=SimpleNamespace(value="default")
        )

        history = await server.get_chat_history("sess")

        # First two turns must be thinking then assistant (order
        # preserved from the inline tag position).
        chat_turns = [t for t in history if t.get("role") in ("thinking", "assistant")]
        assert chat_turns[0]["role"] == "thinking"
        assert chat_turns[0]["content"] == "let me reason"
        assert chat_turns[1]["role"] == "assistant"
        assert chat_turns[1]["content"] == "The answer is 42."

    async def test_get_chat_history_plan_state_pending_when_still_in_plan_mode(
        self,
    ) -> None:
        # Same as the above shape but the session is still in plan
        # mode → the LATEST plan must restore as "pending" (user
        # hasn't approved yet), not "approved".
        from ember_code.backend.server import BackendServer

        assistant_msg = SimpleNamespace(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_p2",
                    "type": "function",
                    "function": {
                        "name": "exit_plan_mode",
                        "arguments": json.dumps({"plan": "Live pending plan."}),
                    },
                }
            ],
            reasoning_content=None,
            from_history=False,
            created_at=10,
        )
        tool_msg = SimpleNamespace(
            role="tool",
            content="Plan submitted.",
            tool_name="exit_plan_mode",
            tool_args=None,
            tool_call_id="call_p2",
            tool_call_error=False,
            from_history=False,
            created_at=11,
        )
        run = SimpleNamespace(
            run_id="r1",
            parent_run_id=None,
            messages=[assistant_msg, tool_msg],
            metrics=None,
        )
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.user_id = "u"
        server._session.main_team.aget_session = AsyncMock(return_value=SimpleNamespace(runs=[run]))
        server._session.permission_evaluator = SimpleNamespace(mode=SimpleNamespace(value="plan"))

        history = await server.get_chat_history("sess")

        plan_turns = [t for t in history if t.get("role") == "plan"]
        assert len(plan_turns) == 1
        assert plan_turns[0]["state"] == "pending"

    async def test_seeds_todo_store_too(self) -> None:
        runs = [
            _run(
                [
                    _assistant_msg_with_tool_call(
                        "exit_plan_mode",
                        {
                            "plan": "Plan with checklist.",
                            "tasks": [
                                {"content": "First step", "status": "pending"},
                                {"content": "Second step", "status": "pending"},
                            ],
                        },
                    ),
                ]
            )
        ]
        server, store = _make_backend_with_runs(runs)

        await server._rehydrate_plan_store()

        assert store.latest == "Plan with checklist."
        server._session.todo_store.set.assert_called_once()
        # Items passed should reflect both tasks.
        passed = server._session.todo_store.set.call_args.args[0]
        assert len(passed) == 2
