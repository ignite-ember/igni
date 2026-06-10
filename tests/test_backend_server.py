"""Tests for the BackendServer — validates the BE protocol layer."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ember_code.protocol import messages as msg


class TestBackendServerCommands:
    """Test that commands return proper protocol messages."""

    @pytest.mark.asyncio
    async def test_handle_command_returns_command_result(self):
        from ember_code.backend.server import BackendServer

        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._settings = MagicMock()
            server._pending_requirements = {}
            server._processing = False

            # Mock the command handler
            mock_result = MagicMock()
            mock_result.kind = "info"
            mock_result.content = "test output"
            mock_result.action = ""

            with patch("ember_code.backend.command_handler.CommandHandler") as MockHandler:
                instance = MockHandler.return_value
                instance.handle = AsyncMock(return_value=mock_result)

                result = await server.handle_command("/test")

            assert isinstance(result, msg.CommandResult)
            assert result.kind == "info"
            assert result.content == "test output"

    @pytest.mark.asyncio
    async def test_get_status(self):
        from ember_code.backend.server import BackendServer

        with patch("ember_code.backend.server.BackendServer.__init__", return_value=None):
            server = BackendServer.__new__(BackendServer)
            server._settings = MagicMock()
            server._settings.models.default = "test-model"
            server._session = MagicMock()
            server._session.cloud_connected = True
            server._session.cloud_org_name = "Test Org"

            status = server.get_status()

        assert isinstance(status, msg.StatusUpdate)
        assert status.model == "test-model"
        assert status.cloud_connected is True
        assert status.cloud_org == "Test Org"

    @pytest.mark.asyncio
    async def test_switch_model(self):
        from ember_code.backend.server import BackendServer

        with (
            patch("ember_code.backend.server.BackendServer.__init__", return_value=None),
            patch("ember_code.core.config.settings.save_default_model"),
        ):
            server = BackendServer.__new__(BackendServer)
            server._session = MagicMock()
            server._session._build_main_agent = MagicMock()
            server._session.session_id = "test-sess"
            server._settings = MagicMock()
            server._session_prefs = MagicMock()

            result = server.switch_model("new-model")

        assert isinstance(result, msg.Info)
        assert "new-model" in result.text
        server._session._build_main_agent.assert_called_once()
        server._session_prefs.set_model.assert_called_once_with("test-sess", "new-model")


class TestCloseModelHttpClient:
    """Test that _close_model_http_client properly closes and replaces the httpx client."""

    @pytest.mark.asyncio
    async def test_closes_async_client(self):
        from ember_code.backend.server import BackendServer

        client = httpx.AsyncClient()
        model = MagicMock()
        model.http_client = client
        team = MagicMock()
        team.model = model

        await BackendServer._close_model_http_client(team)

        assert client.is_closed
        assert isinstance(model.http_client, httpx.AsyncClient)
        assert model.http_client is not client  # replaced with a fresh one

    @pytest.mark.asyncio
    async def test_fresh_client_has_correct_limits(self):
        from ember_code.backend.server import BackendServer

        client = httpx.AsyncClient()
        model = MagicMock()
        model.http_client = client
        team = MagicMock()
        team.model = model

        await BackendServer._close_model_http_client(team)

        new_client = model.http_client
        pool = new_client._transport._pool
        assert pool._max_connections == 10
        assert pool._max_keepalive_connections == 5

    @pytest.mark.asyncio
    async def test_noop_when_no_model(self):
        """Should not raise when team has no model."""
        from ember_code.backend.server import BackendServer

        team = MagicMock(spec=[])  # no model attribute
        await BackendServer._close_model_http_client(team)  # should not raise

    @pytest.mark.asyncio
    async def test_noop_when_no_http_client(self):
        """Should not raise when model has no http_client."""
        from ember_code.backend.server import BackendServer

        model = MagicMock()
        model.http_client = None
        team = MagicMock()
        team.model = model

        await BackendServer._close_model_http_client(team)  # should not raise

    @pytest.mark.asyncio
    async def test_noop_when_sync_client(self):
        """Should not close a sync httpx.Client (only async)."""
        from ember_code.backend.server import BackendServer

        client = httpx.Client()
        model = MagicMock()
        model.http_client = client
        team = MagicMock()
        team.model = model

        await BackendServer._close_model_http_client(team)

        assert not client.is_closed  # sync client should be left alone
        client.close()  # cleanup


class TestProtocolSerialization:
    """Test that protocol messages serialize/deserialize correctly."""

    def test_content_delta_roundtrip(self):
        original = msg.ContentDelta(text="hello world", is_thinking=True)
        json_str = original.model_dump_json()
        restored = msg.ContentDelta.model_validate_json(json_str)
        assert restored.text == "hello world"
        assert restored.is_thinking is True
        assert restored.type == "content_delta"

    def test_tool_completed_with_diff_rows(self):
        rows = [("+ 1   added line", "#69db7c on #003d00"), ("  2   context", "")]
        original = msg.ToolCompleted(
            summary="Edited file.py",
            has_markup=True,
            diff_rows=rows,
        )
        json_str = original.model_dump_json()
        restored = msg.ToolCompleted.model_validate_json(json_str)
        assert restored.diff_rows == rows
        assert restored.has_markup is True

    def test_hitl_request_serialization(self):
        original = msg.HITLRequest(
            requirement_id="abc123",
            tool_name="run_shell_command",
            friendly_name="Bash",
            tool_args={"command": "rm -rf /"},
        )
        json_str = original.model_dump_json()
        restored = msg.HITLRequest.model_validate_json(json_str)
        assert restored.requirement_id == "abc123"
        assert restored.tool_args["command"] == "rm -rf /"

    def test_all_message_types_have_type_field(self):
        """Every concrete message class should have a default type value."""
        import inspect

        for name, cls in inspect.getmembers(msg, inspect.isclass):
            if issubclass(cls, msg.Message) and cls is not msg.Message:
                instance = cls()
                assert instance.type, f"{name} has no default type"

    def test_unix_socket_deserializer(self):
        from ember_code.transport.unix_socket import deserialize_message

        # Round-trip through JSON string
        original = msg.UserMessage(text="hello")
        json_line = original.model_dump_json()
        restored = deserialize_message(json_line)
        assert isinstance(restored, msg.UserMessage)
        assert restored.text == "hello"
