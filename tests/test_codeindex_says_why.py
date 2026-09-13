"""CodeIndex says which of its four "no answer" cases it hit.

``RepositoryResolver.resolve`` returns ``None`` for four unrelated
reasons — no git remote, no cloud token, an unreachable server, a
reply it could not read. All four arrived at the controller as the
same empty cache, which became ``install_state="unknown"``, which the
footer pill rendered as **"not indexed — HEAD needs a sync"**.

A sync fixes none of them. And ``unknown`` is documented as transient
(the controller fires a background resolve and expects the next poll
to have the answer) — but on a machine that is not logged in the
resolve returns early every time, so the state never moves. Observed:
five polls, two seconds apart, all ``unknown``, on a repository with a
perfectly good remote.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx

from ember_code.core.code_index.resolver import RepositoryResolver


def _resolver(tmp_path: Path, *, token: str = "t", remote: str | None = "https://x/y.git"):
    credentials = MagicMock()
    credentials.access_token = token
    resolver = RepositoryResolver(
        project_dir=tmp_path,
        server_url="https://example.invalid",
        credentials=credentials,
    )
    resolver.remote_url = lambda: remote  # type: ignore[method-assign]
    return resolver


async def test_a_folder_with_no_remote_says_so(tmp_path):
    resolver = _resolver(tmp_path, remote=None)

    assert await resolver.resolve() is None
    assert "no git remote" in resolver.failure.reason
    assert resolver.failure.fix


async def test_not_logged_in_says_so(tmp_path):
    """The case on every machine that has not run /login — which is
    every machine on a first run, and was indistinguishable from "your
    index is stale"."""
    resolver = _resolver(tmp_path, token="")

    assert await resolver.resolve() is None
    assert "not logged in" in resolver.failure.reason
    assert "/login" in resolver.failure.fix


async def test_an_unreachable_server_says_so(tmp_path, monkeypatch):
    resolver = _resolver(tmp_path)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Client())

    assert await resolver.resolve() is None
    assert "could not reach" in resolver.failure.reason
    # Worth saying, because it is the only part of igni that needs the
    # server, and a user watching this fail may assume the rest is down.
    assert "offline" in resolver.failure.fix


async def test_expired_credentials_are_not_reported_as_a_server_error(tmp_path, monkeypatch):
    """401 is the one a user can act on, and it was folded in with
    every other non-200."""
    resolver = _resolver(tmp_path)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_args, **_kwargs):
            return httpx.Response(401)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Client())

    assert await resolver.resolve() is None
    assert "rejected" in resolver.failure.reason
    assert "/login" in resolver.failure.fix


async def test_a_reply_it_cannot_read_blames_the_version_gap(tmp_path, monkeypatch):
    resolver = _resolver(tmp_path)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_args, **_kwargs):
            return httpx.Response(200, json={"unexpected": "shape"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Client())

    assert await resolver.resolve() is None
    assert "could not read" in resolver.failure.reason


async def test_success_clears_a_previous_failure(tmp_path, monkeypatch):
    """A stale reason is its own kind of lie: the panel would explain
    a problem that had since been fixed."""
    resolver = _resolver(tmp_path, token="")
    await resolver.resolve()
    assert resolver.failure is not None

    resolver.credentials.access_token = "now-i-have-one"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_args, **_kwargs):
            return httpx.Response(200, json={"status": "registered", "repository_id": "r1"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Client())

    resolved = await resolver.resolve(force=True)

    assert resolved is not None
    assert resolver.failure is None


def test_a_fresh_resolver_reports_no_failure(tmp_path):
    """ "Has not run yet" is not "went wrong"."""
    assert _resolver(tmp_path).failure is None
