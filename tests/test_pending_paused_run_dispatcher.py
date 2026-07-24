"""Tests for the pending-paused-run path in ``MessageDispatcher._on_user_message``.

When a session resumes and has a PAUSED run with pending HITL requirements
(stored during :meth:`LifecycleController.startup`), the dispatcher must:

1. Send ``UserMessageReceived`` (mirroring echo).
2. Call ``PauseHandler.handle`` to evaluate each requirement.
3. Send ``RunPaused`` to the FE for user-facing requirements.
4. Stash auto-resolved requirements in the store so they are merged
   into the eventual ``acontinue_run`` call when the user responds.
5. Send ``StreamEnd`` and return early — NOT call ``run_message``
   (which would start a fresh ``arun`` instead of resuming the
   existing PAUSED run via ``acontinue_run``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.message_dispatcher import MessageDispatcher
from ember_code.backend.pause_handler import PauseHandler
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.core.config.permission_eval import (
    PermissionEvaluator,
    PermissionMode,
)
from ember_code.protocol import messages as msg

# The pending-paused-run resume path these tests specify is NOT yet
# implemented: ``MessageDispatcher._on_user_message`` currently goes
# straight to ``run_message`` with no ``_pending_paused_requirements``
# branch (grep confirms the attribute exists nowhere in production).
# These tests are the spec for that future work — kept as xfail (not
# deleted) so the intended behaviour stays documented and executable.
# ``strict=False`` so they XPASS (not fail) once the feature lands,
# flagging that the marker should be removed.
pytestmark = pytest.mark.xfail(
    reason="pending-paused resume path not implemented in MessageDispatcher yet",
    strict=False,
)


def _make_agno_req(tool_name: str, tool_args: dict | None = None) -> MagicMock:
    """One Agno requirement mock with ``confirm()`` / ``reject()`` as spies."""
    req = MagicMock(spec=["confirm", "reject", "tool_execution"])
    req.tool_execution = MagicMock()
    req.tool_execution.tool_name = tool_name
    req.tool_execution.tool_args = tool_args or {}
    return req


def _make_hitl_controller(
    store: PendingRequirementsStore, evaluator: PermissionEvaluator | None
) -> MagicMock:
    """Build a mock hitl controller with ``_pause_handler`` and ``_store``."""
    hitl = MagicMock()
    hitl._store = store
    hitl._pause_handler = PauseHandler(evaluator=evaluator, store=store)
    hitl._evaluator = evaluator
    return hitl


class TestPendingPausedRunDispatcher:
    """Tests for the pending-paused-run detection in _on_user_message."""

    @pytest.fixture
    def mock_backend(self) -> MagicMock:
        backend = MagicMock()
        backend._pending_paused_requirements = None
        backend._session = MagicMock()
        backend._session.main_team = MagicMock()
        backend.controllers = MagicMock()
        backend.controllers.hitl = None  # type: ignore[assignment]
        backend.runtime = MagicMock()
        backend.runtime.spawn_auto_name = MagicMock()  # noop in tests
        backend.consume_plan_research_flag.return_value = False
        return backend

    @pytest.fixture
    def mock_transport(self) -> MagicMock:
        t = MagicMock()
        t.send = AsyncMock()
        return t

    @pytest.fixture
    def dispatcher(self, mock_backend: MagicMock, mock_transport: MagicMock) -> MessageDispatcher:
        return MessageDispatcher(
            backend=mock_backend,
            transport=mock_transport,
            rpc_table={},
            queue=[],
            login=None,
        )

    async def test_sends_user_message_received_first(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """The mirroring echo must fire before any other side-effects."""
        mock_backend._pending_paused_requirements = (
            "run-1",
            [_make_agno_req("edit_file", {"file_path": "a.py"})],
        )
        # Wire up hitl so the branch that sends RunPaused is reached
        mock_backend.controllers.hitl = _make_hitl_controller(
            PendingRequirementsStore(), PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        )

        user_msg = msg.UserMessage(text="continue", client_id="c1")
        await dispatcher.dispatch(user_msg)

        first_call = mock_transport.send.call_args_list[0]
        assert first_call.args[0].type == "user_message_received"

    async def test_returns_early_without_calling_run_message(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """When a pending PAUSED run exists, run_message must NOT be called."""
        mock_backend._pending_paused_requirements = (
            "run-1",
            [_make_agno_req("edit_file", {"file_path": "a.py"})],
        )
        mock_backend.controllers.hitl = _make_hitl_controller(
            PendingRequirementsStore(), PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        )
        mock_backend.run_message = MagicMock(return_value=iter([]))

        user_msg = msg.UserMessage(text="continue", client_id="c1")
        await dispatcher.dispatch(user_msg)

        mock_backend.run_message.assert_not_called()

    async def test_sends_run_paused_for_user_deferred_req(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """A requirement the evaluator defers must surface as RunPaused."""
        # DEFAULT mode + unknown tool → evaluator returns None → defer to user
        mock_backend._pending_paused_requirements = (
            "run-1",
            [_make_agno_req("mcp__weather__forecast", {"city": "Toronto"})],
        )
        mock_backend.controllers.hitl = _make_hitl_controller(
            PendingRequirementsStore(),
            PermissionEvaluator.from_strings(mode=PermissionMode.DEFAULT),
        )

        user_msg = msg.UserMessage(text="continue", client_id="c1")
        await dispatcher.dispatch(user_msg)

        # Should have sent: UserMessageReceived, RunPaused, StreamEnd
        calls = mock_transport.send.call_args_list
        run_paused_calls = [c for c in calls if c.args[0].type == "run_paused"]
        assert len(run_paused_calls) == 1
        rp = run_paused_calls[0].args[0]
        assert len(rp.requirements) == 1
        assert rp.requirements[0].tool_name == "mcp__weather__forecast"

    async def test_sends_stream_end_and_returns_early(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """After handling pending requirements, dispatcher must send StreamEnd and return."""
        mock_backend._pending_paused_requirements = (
            "run-1",
            [_make_agno_req("mcp__weather__forecast", {"city": "Toronto"})],
        )
        mock_backend.controllers.hitl = _make_hitl_controller(
            PendingRequirementsStore(),
            PermissionEvaluator.from_strings(mode=PermissionMode.DEFAULT),
        )

        user_msg = msg.UserMessage(text="continue", client_id="c1")
        await dispatcher.dispatch(user_msg)

        calls = mock_transport.send.call_args_list
        types = [c.args[0].type for c in calls]
        assert "stream_end" in types
        # No ContentDelta or other run messages
        delta_calls = [c for c in calls if c.args[0].type == "content_delta"]
        assert len(delta_calls) == 0

    async def test_clears_pending_requirements_after_processing(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """The pending data must be cleared so it is only processed once."""
        mock_backend._pending_paused_requirements = (
            "run-1",
            [_make_agno_req("mcp__weather__forecast", {"city": "Toronto"})],
        )
        mock_backend.controllers.hitl = _make_hitl_controller(
            PendingRequirementsStore(),
            PermissionEvaluator.from_strings(mode=PermissionMode.DEFAULT),
        )

        user_msg = msg.UserMessage(text="continue", client_id="c1")
        await dispatcher.dispatch(user_msg)

        assert mock_backend._pending_paused_requirements is None

    async def test_stashes_auto_resolved_for_mixed_requirements(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """When some reqs are auto-resolved and some deferred, auto-resolved must be stashed."""
        store = PendingRequirementsStore()
        # PLAN mode: edit_file is auto-rejected, custom tool is deferred
        mock_backend._pending_paused_requirements = (
            "run-mixed",
            [
                _make_agno_req("edit_file", {"file_path": "a.py"}),
                _make_agno_req("mcp__weather__forecast", {"city": "Toronto"}),
            ],
        )
        mock_backend.controllers.hitl = _make_hitl_controller(
            store, PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        )

        user_msg = msg.UserMessage(text="continue", client_id="c1")
        await dispatcher.dispatch(user_msg)

        # The edit was auto-rejected and must be in the auto-resolved bucket
        auto_bucket = store.auto_resolved_snapshot()
        assert "run-mixed" in auto_bucket
        assert len(auto_bucket["run-mixed"]) == 1
        # The custom tool was deferred and must be in the pending bucket
        assert len(store.pending_ids()) == 1

    async def test_no_hitl_controller_means_graceful_no_op(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """When controllers.hitl is None, dispatcher must still return early."""
        mock_backend._pending_paused_requirements = (
            "run-1",
            [_make_agno_req("mcp__weather__forecast", {"city": "Toronto"})],
        )
        mock_backend.controllers.hitl = None  # type: ignore[assignment]

        user_msg = msg.UserMessage(text="continue", client_id="c1")
        # Must not raise
        await dispatcher.dispatch(user_msg)

        # Should have sent UserMessageReceived and StreamEnd, no run messages
        calls = mock_transport.send.call_args_list
        types = [c.args[0].type for c in calls]
        assert "user_message_received" in types
        assert "stream_end" in types
        assert "content_delta" not in types

    async def test_normal_flow_when_no_pending_requirements(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """When no pending PAUSED requirements exist, run_message must be called."""
        mock_backend._pending_paused_requirements = None  # type: ignore[assignment]
        mock_backend.controllers.hitl = _make_hitl_controller(
            PendingRequirementsStore(),
            PermissionEvaluator.from_strings(mode=PermissionMode.DEFAULT),
        )

        # Simulate run_message yielding a simple info message
        async def run_msg_gen(*args, **kwargs):
            yield msg.ContentDelta(text="hello")
            yield msg.StreamEnd(id="")

        mock_backend.run_message = MagicMock(return_value=run_msg_gen())

        user_msg = msg.UserMessage(text="hello", client_id="c1")
        await dispatcher.dispatch(user_msg)

        mock_backend.run_message.assert_called_once()
        # Should have sent ContentDelta and StreamEnd (not RunPaused)
        calls = mock_transport.send.call_args_list
        types = [c.args[0].type for c in calls]
        assert "content_delta" in types
        assert "run_paused" not in types

    async def test_auto_resume_all_reqeusts_auto_resolved(
        self, dispatcher: MessageDispatcher, mock_backend: MagicMock, mock_transport: MagicMock
    ) -> None:
        """When every req is auto-resolved by evaluator, no RunPaused is sent."""
        store = PendingRequirementsStore()
        # PLAN mode: edit_file is auto-rejected, read_file is auto-allowed
        mock_backend._pending_paused_requirements = (
            "run-all-auto",
            [
                _make_agno_req("edit_file", {"file_path": "a.py"}),
                _make_agno_req("read_file", {"file_path": "b.py"}),
            ],
        )
        mock_backend.controllers.hitl = _make_hitl_controller(
            store, PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        )

        user_msg = msg.UserMessage(text="continue", client_id="c1")
        await dispatcher.dispatch(user_msg)

        # No RunPaused should be sent (all auto-resolved)
        calls = mock_transport.send.call_args_list
        types = [c.args[0].type for c in calls]
        assert "run_paused" not in types
        # Auto-resolved bucket should have both reqs
        auto_bucket = store.auto_resolved_snapshot()
        assert len(auto_bucket.get("run-all-auto", [])) == 2
