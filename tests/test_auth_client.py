"""Tests for the browser-based CLI authentication flow.

Exercises the OOP-first surface in
:mod:`ember_code.core.auth.portal_client` and
:mod:`ember_code.core.auth.callback_server`.
"""

from threading import Thread
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


class TestTheCallbackContract:
    """What the portal actually sends, driven over HTTP.

    Every other test in this class calls ``_deliver_token`` directly,
    which is why the handler's query-parameter contract survived F107
    untested: the portal stopped putting a token in the URL for the
    *browser* sign-in and this path kept doing it, and no test noticed
    because no test ever made a request.

    So these make real HTTP requests to the real handler.
    """

    @staticmethod
    def _get(url: str) -> tuple[int, str]:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_a_code_is_exchanged_for_the_token(self, monkeypatch):
        """The whole point: the URL carries a one-time code, and the
        token arrives over POST."""
        import httpx

        exchanged: dict[str, object] = {}

        def fake_post(url, json=None, timeout=None):  # noqa: ANN001, ARG001
            exchanged["url"] = url
            exchanged["code"] = (json or {}).get("code")
            return httpx.Response(200, json={"access_token": "a-real-token"})

        monkeypatch.setattr(httpx, "post", fake_post)

        cb = CallbackServer(api_url="https://api.example.test")
        try:
            thread = Thread(target=cb._serve, daemon=True)
            thread.start()
            status, body = self._get(f"{cb.callback_url}?code=one-time-code")
        finally:
            cb.stop()

        assert status == 200
        assert exchanged["url"] == "https://api.example.test/v1/auth/exchange"
        assert exchanged["code"] == "one-time-code"
        assert cb._token == "a-real-token"
        assert "a-real-token" not in body, "the token must not be written into the page"

    def test_a_token_in_the_url_is_refused(self, monkeypatch):
        """The old contract must stop working.

        Left accepting both, every portal that had not been redeployed
        would keep putting a credential in the address bar and nothing
        would say so.
        """
        import httpx

        monkeypatch.setattr(
            httpx, "post", lambda *a, **k: pytest.fail("no exchange should be attempted")
        )

        cb = CallbackServer(api_url="https://api.example.test")
        try:
            thread = Thread(target=cb._serve, daemon=True)
            thread.start()
            status, body = self._get(f"{cb.callback_url}?token=a-bare-jwt")
        finally:
            cb.stop()

        assert status == 400
        assert cb._token is None
        assert "did not complete" in body

    def test_a_refused_code_says_so_without_saying_which_way(self, monkeypatch):
        """Expired, already used and never existed get one message.

        The difference matters to nobody except somebody probing, for
        whom "that one was valid a moment ago" is a useful signal — the
        same reasoning as the server's own exchange endpoint.
        """
        import httpx

        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: httpx.Response(400, json={"detail": "This sign-in code is not valid."}),
        )

        cb = CallbackServer(api_url="https://api.example.test")
        try:
            thread = Thread(target=cb._serve, daemon=True)
            thread.start()
            status, body = self._get(f"{cb.callback_url}?code=stale")
        finally:
            cb.stop()

        assert status == 400
        assert cb._token is None
        assert "expired" in body and "already used" in body

    def test_an_unreachable_server_does_not_hang_the_login(self, monkeypatch):
        """The code lives 30 seconds. A hang here spends all of it and
        leaves the user with a spinner and no explanation."""
        import httpx

        def refuse(*_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", refuse)

        cb = CallbackServer(api_url="https://api.example.test")
        try:
            thread = Thread(target=cb._serve, daemon=True)
            thread.start()
            status, _ = self._get(f"{cb.callback_url}?code=whatever")
        finally:
            cb.stop()

        assert status == 400
        assert cb._token is None

    def test_no_api_url_cannot_silently_succeed(self):
        """A server built without somewhere to redeem must refuse
        rather than deliver something unverified."""
        cb = CallbackServer()
        try:
            thread = Thread(target=cb._serve, daemon=True)
            thread.start()
            status, _ = self._get(f"{cb.callback_url}?code=one-time-code")
        finally:
            cb.stop()

        assert status == 400
        assert cb._token is None


class TestCallbackServer:
    def test_exposes_port_and_callback_url_as_instance_state(self):
        cb = CallbackServer()
        try:
            assert isinstance(cb.port, int)
            assert cb.port > 0
            # 127.0.0.1, matching what the server binds. It said
            # ``localhost``, which resolves to ::1 first on a
            # dual-stack machine — a connection refused on the one
            # step of a login the user cannot retry. F124.
            assert cb.callback_url == f"http://127.0.0.1:{cb.port}/callback"
            assert "127.0.0.1" in cb.callback_url
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
