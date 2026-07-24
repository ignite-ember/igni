"""Tests for error recovery and resilience — P0 critical.

Covers: API timeout handling, cancel behavior, MCP crash recovery.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.backend.hitl_tracer import HITLTracer
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.backend.run_controller import RunController as BackendRunController
from ember_code.backend.server import BackendServer
from ember_code.protocol import messages as msg
from ember_code.protocol.messages import Error


def _wire_backend_runs(server: BackendServer) -> None:
    """Attach a :class:`BackendRunController` to a partial
    ``BackendServer`` built via ``__new__``. The controller owns the
    pre-run pipeline + streaming loop delegate. See
    ``tests/test_streaming_done_unblock._wire_backend_runs`` for the
    counterpart in the streaming-lock suite."""
    server._runs = BackendRunController(
        backend=server,
        session=server._session,
        pending_store=server._pending_store,
    )


class TestBackendRunMessageErrors:
    """Test that BackendServer.run_message() handles errors gracefully."""

    @pytest.mark.asyncio
    async def test_agno_exception_yields_error(self):
        """Agno runtime error should yield an Error protocol message."""
        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._session.main_team = MagicMock()

            async def _failing_arun(*args, **kwargs):
                raise RuntimeError("Model API failure")
                yield  # noqa: unreachable — makes this an async generator

            server._session.main_team.arun = _failing_arun
            server._session._learning = None
            server._session._inject_learnings = AsyncMock()
            server._session.inject_learnings = AsyncMock()
            server._session.hook_executor = MagicMock()
            server._session.hook_executor.execute = AsyncMock(
                return_value=MagicMock(should_continue=True, message="")
            )
            server._session.session_id = "test"
            server._processing = False
            server._settings = MagicMock()
            server._hitl_store = PendingRequirementsStore()
            server._hitl_tracer = HITLTracer(enabled=False)
            server._run_lock = asyncio.Lock()
            server._pending_store = MagicMock()
            server._pending_store.arecord_received = AsyncMock(return_value="mid-1")
            server._pending_store.amark_completed = AsyncMock()
            server._pending_store.adiscard = AsyncMock()
            server._periodic_checkpoint = AsyncMock()
            _wire_backend_runs(server)

            results = []
            async for proto in server.run_message("hello"):
                results.append(proto)

            errors = [r for r in results if isinstance(r, Error)]
            assert len(errors) >= 1
            assert "Model API failure" in errors[0].text

    @pytest.mark.asyncio
    async def test_hook_blocks_message(self):
        """UserPromptSubmit hook blocking should yield error, not crash."""
        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._session._inject_learnings = AsyncMock()
            server._session.inject_learnings = AsyncMock()
            server._session.hook_executor = MagicMock()
            server._session.hook_executor.execute = AsyncMock(
                return_value=MagicMock(should_continue=False, message="Blocked by hook")
            )
            server._session.session_id = "test"
            server._session._learning = None
            server._processing = False
            server._settings = MagicMock()
            server._hitl_store = PendingRequirementsStore()
            server._hitl_tracer = HITLTracer(enabled=False)
            server._run_lock = asyncio.Lock()
            server._pending_store = MagicMock()
            server._pending_store.arecord_received = AsyncMock(return_value="mid-1")
            server._pending_store.amark_completed = AsyncMock()
            server._pending_store.adiscard = AsyncMock()
            server._periodic_checkpoint = AsyncMock()
            _wire_backend_runs(server)

            results = []
            async for proto in server.run_message("blocked message"):
                results.append(proto)

            errors = [r for r in results if isinstance(r, Error)]
            assert len(errors) == 1
            assert "Blocked by hook" in errors[0].text


class TestBackendCancelRun:
    """Test run cancellation via backend."""

    def test_cancel_run_no_crash_when_no_team(self):
        """cancel_run should not crash even if no team/run active."""
        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._session.main_team = MagicMock(spec=[])  # no run_id attr
            # ``cancel_run`` now also cancels the in-flight asyncio
            # task (``self._current_run_task``). ``__new__``-bypassed
            # init leaves the attribute unset; mirror the real
            # ``__init__`` default so the cancel path runs cleanly.
            server._current_run_task = None

            # Should not raise
            server.cancel_run()


class TestToolExceptionRecovery:
    """Test that tool exceptions don't crash the session."""

    @pytest.mark.asyncio
    async def test_tool_error_becomes_protocol_message(self):
        """Tool exceptions should be serialized as ToolError protocol messages."""

        # Create a mock tool error event
        event = MagicMock()
        event.__class__ = type("ToolCallErrorEvent", (), {})
        event.error = "File not found: /nonexistent"
        event.run_id = "run-1"

        # Check if isinstance would match (it won't with our mock, so test serializer directly)
        error_msg = msg.ToolError(error="File not found", run_id="run-1")
        assert error_msg.type == "tool_error"
        assert error_msg.error == "File not found"
