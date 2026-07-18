"""Tests for session/runner.py — single-message session execution."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.session.runner import run_single_message


def _make_mock_session():
    """Build a MagicMock stand-in for :class:`Session` with a
    stub :class:`DisplayManager` on ``.display``.

    The refactor routes every terminal print through
    ``session.display.print_*``, so tests observe rendered
    output by inspecting the mock display's method calls instead
    of patching module-level free functions (which no longer
    exist on ``session.runner``)."""
    mock_session = MagicMock()
    mock_session.session_id = "test-123"
    mock_session.settings = MagicMock()
    # ``settings.models.default`` flows into a Pydantic ``RunStats``
    # model in ``runner.py`` — MagicMock's auto-attr default is a
    # Mock, which fails ``str`` validation. Pin to a real string.
    mock_session.settings.models.default = "test-model"
    mock_session.hook_executor.execute = AsyncMock()
    mock_session.handle_message = AsyncMock(return_value="Hello!")
    mock_session.display = MagicMock()
    return mock_session


class TestRunSingleMessage:
    @pytest.mark.asyncio
    async def test_runs_message_and_prints_response(self, tmp_path):
        with patch("ember_code.core.session.session_run.Session") as MockSession:
            mock_session = _make_mock_session()
            MockSession.return_value = mock_session

            settings = MagicMock()
            await run_single_message(settings, "Hi there")

            mock_session.handle_message.assert_called_once_with("Hi there")
            mock_session.display.print_response.assert_called_once_with("Hello!")

    @pytest.mark.asyncio
    async def test_fires_session_start_and_end_hooks(self, tmp_path):
        with patch("ember_code.core.session.session_run.Session") as MockSession:
            mock_session = _make_mock_session()
            mock_session.session_id = "s1"
            mock_session.handle_message = AsyncMock(return_value="ok")
            MockSession.return_value = mock_session

            await run_single_message(MagicMock(), "test")

            calls = mock_session.hook_executor.execute.call_args_list
            events = [c[1]["event"] for c in calls]
            assert "SessionStart" in events
            assert "SessionEnd" in events

    @pytest.mark.asyncio
    async def test_passes_project_dir_and_additional_dirs(self):
        with patch("ember_code.core.session.session_run.Session") as MockSession:
            mock_session = _make_mock_session()
            mock_session.session_id = "s1"
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
