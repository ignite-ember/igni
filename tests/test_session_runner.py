"""Tests for session/runner.py — single-message session execution."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.session.runner import run_single_message


class TestRunSingleMessage:
    @pytest.mark.asyncio
    async def test_runs_message_and_prints_response(self, tmp_path):
        with (
            patch("ember_code.core.session.runner.Session") as MockSession,
            patch("ember_code.core.session.runner.print_response") as mock_print,
            patch("ember_code.core.session.runner.print_run_stats"),
        ):
            mock_session = MagicMock()
            mock_session.session_id = "test-123"
            mock_session.settings = MagicMock()
            mock_session.hook_executor.execute = AsyncMock()
            mock_session.handle_message = AsyncMock(return_value="Hello!")
            MockSession.return_value = mock_session

            settings = MagicMock()
            await run_single_message(settings, "Hi there")

            mock_session.handle_message.assert_called_once_with("Hi there")
            mock_print.assert_called_once_with("Hello!")

    @pytest.mark.asyncio
    async def test_fires_session_start_and_end_hooks(self, tmp_path):
        with (
            patch("ember_code.core.session.runner.Session") as MockSession,
            patch("ember_code.core.session.runner.print_response"),
            patch("ember_code.core.session.runner.print_run_stats"),
        ):
            mock_session = MagicMock()
            mock_session.session_id = "s1"
            mock_session.settings = MagicMock()
            mock_session.hook_executor.execute = AsyncMock()
            mock_session.handle_message = AsyncMock(return_value="ok")
            MockSession.return_value = mock_session

            await run_single_message(MagicMock(), "test")

            calls = mock_session.hook_executor.execute.call_args_list
            events = [c[1]["event"] for c in calls]
            assert "SessionStart" in events
            assert "SessionEnd" in events

    @pytest.mark.asyncio
    async def test_passes_project_dir_and_additional_dirs(self):
        with (
            patch("ember_code.core.session.runner.Session") as MockSession,
            patch("ember_code.core.session.runner.print_response"),
            patch("ember_code.core.session.runner.print_run_stats"),
        ):
            mock_session = MagicMock()
            mock_session.session_id = "s1"
            mock_session.settings = MagicMock()
            mock_session.hook_executor.execute = AsyncMock()
            mock_session.handle_message = AsyncMock(return_value="ok")
            MockSession.return_value = mock_session

            await run_single_message(
                MagicMock(),
                "test",
                project_dir=Path("/tmp/proj"),
                additional_dirs=[Path("/tmp/extra")],
            )

            call_kwargs = MockSession.call_args[1]
            assert call_kwargs["project_dir"] == Path("/tmp/proj")
            assert call_kwargs["additional_dirs"] == [Path("/tmp/extra")]
