"""Tests for the browser-based CLI authentication flow.

Exercises the OOP-first surface in
:mod:`ember_code.core.auth.portal_client` and
:mod:`ember_code.core.auth.callback_server`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.auth.callback_server import CallbackServer
from ember_code.core.auth.portal_client import PortalClient
from ember_code.core.auth.schemas import ValidateResult


class TestPortalClientLoginUrl:
    def test_default_portal(self):
        url = PortalClient().login_url(9999)
        assert "ignite-ember.sh" in url
        assert "cli-auth" in url
        assert "port=9999" in url

    def test_custom_portal(self):
        url = PortalClient(portal_url="https://portal.test.com").login_url(9999)
        assert url == "https://portal.test.com/cli-auth?port=9999"

    def test_strips_trailing_slash(self):
        url = PortalClient(portal_url="https://portal.test.com/").login_url(9999)
        assert url == "https://portal.test.com/cli-auth?port=9999"


class TestFindFreePort:
    def test_returns_int(self):
        port = CallbackServer._find_free_port()
        assert isinstance(port, int)
        assert port > 0

    def test_returns_different_ports(self):
        ports = {CallbackServer._find_free_port() for _ in range(5)}
        assert len(ports) >= 2


class TestCallbackServer:
    def test_exposes_port_and_callback_url_as_instance_state(self):
        cb = CallbackServer()
        try:
            assert isinstance(cb.port, int)
            assert cb.port > 0
            assert cb.callback_url == f"http://localhost:{cb.port}/callback"
            assert "localhost" in cb.callback_url
            assert "/callback" in cb.callback_url
        finally:
            cb.stop()

    def test_stop_is_idempotent(self):
        cb = CallbackServer()
        cb.stop()
        cb.stop()


class TestPortalClientValidateToken:
    @pytest.mark.asyncio
    async def test_valid_token_returns_ok_result(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"email": "user@test.com", "name": "Test User"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        portal = PortalClient(api_url="https://api.test.com")
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await portal.validate_token("valid-token")

        assert isinstance(result, ValidateResult)
        assert result.ok is True
        assert result.reason == "ok"
        assert result.status_code == 200
        assert result.user is not None
        assert result.user.email == "user@test.com"

    @pytest.mark.asyncio
    async def test_http_error_returns_http_error_result(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        portal = PortalClient(api_url="https://api.test.com")
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await portal.validate_token("bad-token")

        assert result.ok is False
        assert result.reason == "http_error"
        assert result.status_code == 401
        assert result.user is None

    @pytest.mark.asyncio
    async def test_network_error_returns_network_error_result(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        portal = PortalClient(api_url="https://api.test.com")
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await portal.validate_token("token")

        assert result.ok is False
        assert result.reason == "network_error"
        assert "connection refused" in result.error
        assert result.user is None
