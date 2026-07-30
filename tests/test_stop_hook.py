"""Tests for Stop hook blocking — retries agent when Stop hook rejects response."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.config.settings import Settings
from ember_code.core.session.core import Session

# Reuse the shared patching infrastructure from test_session
from tests.test_session import _session_patches, _start_patches, _stop_patches


class TestStopHookBlocking:
    """Stop hook can reject a response, causing the agent to retry."""

    @pytest.fixture
    def session(self, tmp_path):
        patches = _session_patches()
        _start_patches(patches)

        s = Session(Settings(), project_dir=tmp_path)

        # Configure mocks for message handling
        mock_hook_result = MagicMock()
        mock_hook_result.should_continue = True
        s.hook_executor.execute = AsyncMock(return_value=mock_hook_result)
        s.persistence.auto_name = AsyncMock()
        s.audit.log = MagicMock()

        # Mock the team response
        mock_response = MagicMock()
        mock_response.content = "Hello! I can help."
        mock_response.metrics = None
        s.main_team.arun = AsyncMock(return_value=mock_response)
        s.main_team.run_response = MagicMock(metrics=None)

        yield s
        _stop_patches(patches)

    @pytest.mark.asyncio
    async def test_stop_hook_allows_response(self, session):
        """When Stop hook allows, response is returned normally."""
        with patch(
            "ember_code.core.session.core.extract_response_text", return_value="Good response"
        ):
            result = await session.handle_message("Hi")
            assert result == "Good response"

    @pytest.mark.asyncio
    async def test_stop_hook_blocks_then_allows(self, session):
        """Stop hook blocks first response, agent retries and succeeds."""
        blocked = MagicMock()
        blocked.should_continue = False
        blocked.message = "Response contains forbidden content"

        allowed = MagicMock()
        allowed.should_continue = True

        # UserPromptSubmit allows, Stop blocks first, then allows
        session.hook_executor.execute = AsyncMock(side_effect=[allowed, blocked, allowed])

        call_count = 0
        responses = ["Bad response", "Clean response"]

        def fake_extract(resp):
            nonlocal call_count
            result = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return result

        with patch("ember_code.core.session.core.extract_response_text", side_effect=fake_extract):
            result = await session.handle_message("test")
            assert result == "Clean response"
            # Agent should have been called twice: original + retry
            assert session.main_team.arun.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_hook_blocks_three_times(self, session):
        """Stop hook blocks all 3 retries — last response is returned anyway."""
        blocked = MagicMock()
        blocked.should_continue = False
        blocked.message = "Still bad"

        allowed = MagicMock()
        allowed.should_continue = True

        # UserPromptSubmit allows, then Stop blocks 3 times
        session.hook_executor.execute = AsyncMock(side_effect=[allowed, blocked, blocked, blocked])

        with patch("ember_code.core.session.core.extract_response_text", return_value="response"):
            result = await session.handle_message("test")
            # After 3 blocks, the loop exits and returns the last response
            assert result == "response"
            # Original call + 3 retries = 4 calls total
            assert session.main_team.arun.call_count == 4

    @pytest.mark.asyncio
    async def test_stop_hook_retry_contains_system_message(self, session):
        """When Stop blocks, the retry message includes the rejection reason."""
        blocked = MagicMock()
        blocked.should_continue = False
        blocked.message = "No profanity allowed"

        allowed = MagicMock()
        allowed.should_continue = True

        session.hook_executor.execute = AsyncMock(side_effect=[allowed, blocked, allowed])

        with patch("ember_code.core.session.core.extract_response_text", return_value="ok"):
            await session.handle_message("test")
            # Second call should be the retry with system message
            retry_call = session.main_team.arun.call_args_list[1]
            retry_msg = retry_call[0][0]
            assert "No profanity allowed" in retry_msg
            assert "[SYSTEM]" in retry_msg
