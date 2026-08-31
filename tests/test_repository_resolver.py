"""Tests for RepositoryResolver — git remote read + server discovery."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import httpx
import pytest

from ember_code.core.code_index.resolver import (
    RepositoryResolver,
)


def _stub_credentials(token: str | None = "tok-xyz"):
    creds = MagicMock()
    creds.access_token = token
    return creds


def _git_init_with_remote(path, *, remote_url: str = "https://github.com/acme/widgets") -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=path,
        check=True,
    )


class TestRemoteUrl:
    def test_returns_url_when_origin_is_set(self, tmp_path):
        _git_init_with_remote(tmp_path)
        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        assert resolver.remote_url() == "https://github.com/acme/widgets"

    def test_returns_none_for_non_git_dir(self, tmp_path):
        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        assert resolver.remote_url() is None

    def test_returns_none_when_no_origin(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        assert resolver.remote_url() is None


def _mock_transport(*, status: int, body: dict | bytes = b""):
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, request=request)

    return httpx.MockTransport(handler)


def _patched_async_client(monkeypatch, transport: httpx.MockTransport) -> list[httpx.Request]:
    captured: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        client = real_async_client(*args, **kwargs)

        original_get = client.get

        async def wrapped_get(url, **kw):
            captured.append((url, kw))
            return await original_get(url, **kw)

        client.get = wrapped_get  # type: ignore[method-assign]
        return client

    monkeypatch.setattr("ember_code.core.code_index.resolver.httpx.AsyncClient", factory)
    return captured


class TestResolve:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_remote(self, tmp_path):
        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        assert await resolver.resolve() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_unauthenticated(self, tmp_path):
        _git_init_with_remote(tmp_path)
        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(token=None),
        )
        assert await resolver.resolve() is None

    @pytest.mark.asyncio
    async def test_happy_path_caches_response(self, tmp_path, monkeypatch):
        _git_init_with_remote(tmp_path)
        transport = _mock_transport(
            status=200,
            body={
                "status": "registered",
                "repository_id": "repo-7",
                "install_url": None,
            },
        )
        captured = _patched_async_client(monkeypatch, transport)

        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        first = await resolver.resolve()
        assert first is not None
        assert first.repository_id == "repo-7"
        assert first.needs_install is False
        # Second call hits cache, no new request.
        second = await resolver.resolve()
        assert second is first
        assert len(captured) == 1
        url, kwargs = captured[0]
        assert url == "http://srv/v1/codeindex/repository"
        assert kwargs["params"] == {"remote_url": "https://github.com/acme/widgets"}
        assert kwargs["headers"]["Authorization"] == "Bearer tok-xyz"

    @pytest.mark.asyncio
    async def test_install_required_returns_install_url(self, tmp_path, monkeypatch):
        _git_init_with_remote(tmp_path)
        install_url = "https://github.com/apps/ember-codeindex/installations/new?state=..."
        transport = _mock_transport(
            status=200,
            body={
                "status": "install_required",
                "repository_id": None,
                "install_url": install_url,
            },
        )
        _patched_async_client(monkeypatch, transport)

        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        resolved = await resolver.resolve()
        assert resolved is not None
        assert resolved.needs_install is True
        assert resolved.install_url == install_url
        assert resolved.repository_id is None

    @pytest.mark.asyncio
    async def test_force_refresh_skips_cache(self, tmp_path, monkeypatch):
        _git_init_with_remote(tmp_path)
        transport = _mock_transport(
            status=200,
            body={"status": "registered", "repository_id": "repo-7"},
        )
        captured = _patched_async_client(monkeypatch, transport)

        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        await resolver.resolve()
        await resolver.resolve(force=True)
        assert len(captured) == 2

    @pytest.mark.asyncio
    async def test_403_surfaces_the_reason_and_message(self, tmp_path, monkeypatch):
        """A denial is an answer, not an outage.

        This used to return ``None``, which the sync manager reported as
        "codeindex unavailable" — so the commonest case, a user who has
        simply never linked a GitHub identity, was told to check their
        connection instead of being handed the two-click fix.
        """
        _git_init_with_remote(tmp_path)
        transport = _mock_transport(
            status=403,
            body={
                "detail": {
                    "reason": "no_linked_logins",
                    "message": "Connect one at https://portal/dashboard, then try again.",
                }
            },
        )
        _patched_async_client(monkeypatch, transport)

        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        resolved = await resolver.resolve()

        assert resolved is not None
        assert resolved.access_denied
        assert resolved.denial_reason == "no_linked_logins"
        assert "/dashboard" in resolved.denial_message
        # Not cached: the user fixes this in a browser without restarting
        # igni, and a cached verdict would outlive the problem.
        assert resolver.cached is None

    @pytest.mark.asyncio
    async def test_403_with_a_plain_string_detail_still_reports_denial(self, tmp_path, monkeypatch):
        """An older server, or a proxy that rewrote the body."""
        _git_init_with_remote(tmp_path)
        transport = _mock_transport(status=403, body={"detail": "no access"})
        _patched_async_client(monkeypatch, transport)

        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        resolved = await resolver.resolve()

        assert resolved is not None and resolved.access_denied
        assert resolved.denial_reason is None
        assert resolved.denial_message == "no access"

    @pytest.mark.asyncio
    async def test_403_with_an_unreadable_body_still_reports_denial(self, tmp_path, monkeypatch):
        _git_init_with_remote(tmp_path)
        transport = _mock_transport(status=403, body=b"<html>Forbidden</html>")
        _patched_async_client(monkeypatch, transport)

        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        resolved = await resolver.resolve()

        assert resolved is not None and resolved.access_denied
        assert resolved.denial_message  # a sentence, not an empty string

    @pytest.mark.asyncio
    async def test_5xx_returns_none(self, tmp_path, monkeypatch):
        _git_init_with_remote(tmp_path)
        transport = _mock_transport(status=503, body={"detail": "down"})
        _patched_async_client(monkeypatch, transport)

        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        assert await resolver.resolve() is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self, tmp_path, monkeypatch):
        _git_init_with_remote(tmp_path)

        def boom_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope", request=request)

        transport = httpx.MockTransport(boom_handler)
        _patched_async_client(monkeypatch, transport)

        resolver = RepositoryResolver(
            project_dir=tmp_path,
            server_url="http://srv",
            credentials=_stub_credentials(),
        )
        assert await resolver.resolve() is None
