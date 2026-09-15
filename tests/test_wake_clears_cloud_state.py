"""Waking up forgets what the cloud said before the sleep.

A resolution is an answer about a remote server. After a sleep of any
length it is a claim about the past, and the far end has by then
closed the socket it was obtained over. Holding it means the codeindex
pill shows a pre-sleep answer — "connected", or a stale reason — until
some later poll happens to fail.

Dropping it costs one request. That is the entire reaction, and
deliberately so: the next status poll already knows how to resolve,
report and record, so a wake handler that tried to redo any of that
would be a second implementation of something that works.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx

from ember_code.core.code_index.resolver import RepositoryResolver


def _resolver(tmp_path: Path, *, token: str = "t"):
    credentials = MagicMock()
    credentials.access_token = token
    resolver = RepositoryResolver(
        project_dir=tmp_path,
        server_url="https://example.invalid",
        credentials=credentials,
    )
    resolver.remote_url = lambda: "https://x/y.git"  # type: ignore[method-assign]
    return resolver


def _client(response: httpx.Response):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_args, **_kwargs):
            return response

    return lambda **_kw: _Client()


async def test_a_successful_resolution_is_dropped_on_wake(tmp_path, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client(
            httpx.Response(
                200,
                json={"status": "registered", "repository_id": "r1"},
                request=httpx.Request("GET", "https://cloud.invalid/v1/codeindex/repository"),
            )
        ),
    )
    resolver = _resolver(tmp_path)
    assert await resolver.resolve() is not None
    assert resolver.cached is not None

    resolver.invalidate()

    assert resolver.cached is None


async def test_a_failure_reason_is_dropped_too(tmp_path):
    """ "Not logged in" surviving a wake into a fresh session would be
    its own small lie — the reason describes a situation that may no
    longer exist."""
    resolver = _resolver(tmp_path, token="")
    await resolver.resolve()
    assert resolver.failure is not None

    resolver.invalidate()

    assert resolver.failure is None


async def test_the_next_call_actually_asks_again(tmp_path, monkeypatch):
    """Invalidating has to clear the short-circuit at the top of
    ``resolve``, not merely blank the attribute — otherwise the next
    poll returns the same cached nothing and the wake changed
    nothing."""
    calls = 0

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={"status": "registered", "repository_id": "r1"},
                request=httpx.Request("GET", "https://cloud.invalid/v1/codeindex/repository"),
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Client())
    resolver = _resolver(tmp_path)

    await resolver.resolve()
    await resolver.resolve()  # cached — no second request
    assert calls == 1

    resolver.invalidate()
    await resolver.resolve()

    assert calls == 2
