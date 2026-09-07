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


class TestAnEmptyModelTurnIsNotSilence:
    """A model can return an assistant turn with nothing in it.

    Captured on the wire against a real provider: one round trip,
    ``content: ''``, ``tool_calls: []``, no ``reasoning_content``
    either. The agent loop is right to stop — there is nothing to run
    and nothing to say.

    What was wrong is what the user saw. ``print_response`` hands the
    empty string to ``print_markdown``, which renders nothing, so the
    command printed a timing line and exited 0. That is
    indistinguishable from a crash, a hang, or a question the agent
    decided to ignore, and it is the only failure mode in the product
    with no message anywhere to search for. Reproduced three times out
    of three with one model before the fix, and it is silence every
    time.

    The notice lives in the shared turn pipeline rather than at the
    ``-m`` entry point, because the interactive loop renders the same
    silence through the same call.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("empty", ["", "   ", "\n\n"])
    async def test_an_empty_response_says_so(self, empty):
        with patch("ember_code.core.session.session_run.Session") as MockSession:
            mock_session = _make_mock_session()
            mock_session.handle_message = AsyncMock(return_value=empty)
            MockSession.return_value = mock_session

            await run_single_message(MagicMock(), "anything")

            mock_session.display.print_response.assert_not_called()
            mock_session.display.print_warning.assert_called_once()
            said = mock_session.display.print_warning.call_args[0][0]
            assert "empty response" in said
            # Actionable, or it is a nicer-looking dead end.
            assert "/model" in said

    @pytest.mark.asyncio
    async def test_a_real_response_is_still_rendered_normally(self):
        """Guards the guard: a warning on every turn would be worse
        than the silence it replaced."""
        with patch("ember_code.core.session.session_run.Session") as MockSession:
            mock_session = _make_mock_session()
            mock_session.handle_message = AsyncMock(return_value="the answer")
            MockSession.return_value = mock_session

            await run_single_message(MagicMock(), "anything")

            mock_session.display.print_response.assert_called_once_with("the answer")
            mock_session.display.print_warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_response_that_is_only_whitespace_counts_as_empty(self):
        """A model that answers with a newline has answered nothing.

        Split out because ``if response:`` would pass this one — the
        string is truthy — and the user would still see a blank line
        and a timing figure.
        """
        with patch("ember_code.core.session.session_run.Session") as MockSession:
            mock_session = _make_mock_session()
            mock_session.handle_message = AsyncMock(return_value=" \n\t ")
            MockSession.return_value = mock_session

            await run_single_message(MagicMock(), "anything")

            mock_session.display.print_warning.assert_called_once()
