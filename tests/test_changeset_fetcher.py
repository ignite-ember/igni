"""Tests for the consumer-side ChangesetFetcher (signed-URL flow)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from ember_code.core.code_index.fetcher import (
    ChangesetFetcher,
    ChangesetFetchError,
    PreflightStatus,
)

SIGNED_URL = "https://storage.googleapis.com/test-bucket/changesets/r/abc.jsonl?signed=1"


def _route(*, signed_url_response, blob_response):
    """Build a MockTransport that routes /changeset-url to one response, signed URL to another."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/codeindex/changeset-url"):
            status, body = signed_url_response
            return httpx.Response(status, content=body, request=request)
        if str(request.url) == SIGNED_URL:
            status, body = blob_response
            return httpx.Response(status, content=body, request=request)
        return httpx.Response(500, content=b"unexpected", request=request)

    return httpx.MockTransport(handler)


class TestFetcherConstructor:
    def test_empty_server_url_rejected(self):
        with pytest.raises(ValueError, match="server_url"):
            ChangesetFetcher(server_url="", bearer_token="tok")

    def test_empty_token_rejected(self):
        with pytest.raises(ValueError, match="bearer_token"):
            ChangesetFetcher(server_url="http://srv", bearer_token="")


class TestDownload:
    @pytest.mark.asyncio
    async def test_full_signed_url_flow(self, tmp_path):
        body = b'{"op":"commit","sha":"abc","parent_sha":null}\n'
        transport = _route(
            signed_url_response=(
                200,
                json.dumps(
                    {"signed_url": SIGNED_URL, "expires_at": "2026-04-28T00:10:00Z"}
                ).encode(),
            ),
            blob_response=(200, body),
        )
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            target = await fetcher.download(
                repository_id="r",
                commit_sha="abc",
                dest_dir=tmp_path,
                client=client,
            )
        assert target == tmp_path / "abc.jsonl"
        assert target.read_bytes() == body

    @pytest.mark.asyncio
    async def test_403_from_server_raises(self, tmp_path):
        transport = _route(
            signed_url_response=(403, b'{"detail":"no access"}'),
            blob_response=(500, b"unreachable"),
        )
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ChangesetFetchError, match="access denied"):
                await fetcher.download(
                    repository_id="r",
                    commit_sha="abc",
                    dest_dir=tmp_path,
                    client=client,
                )

    @pytest.mark.asyncio
    async def test_404_from_server_raises(self, tmp_path):
        transport = _route(
            signed_url_response=(404, b'{"detail":"unknown repo"}'),
            blob_response=(500, b"unreachable"),
        )
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ChangesetFetchError, match="not found"):
                await fetcher.download(
                    repository_id="r",
                    commit_sha="abc",
                    dest_dir=tmp_path,
                    client=client,
                )

    @pytest.mark.asyncio
    async def test_signed_url_404_means_not_uploaded_yet(self, tmp_path):
        transport = _route(
            signed_url_response=(
                200,
                json.dumps(
                    {"signed_url": SIGNED_URL, "expires_at": "2026-04-28T00:10:00Z"}
                ).encode(),
            ),
            blob_response=(404, b"NoSuchKey"),
        )
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ChangesetFetchError, match="has not been uploaded yet"):
                await fetcher.download(
                    repository_id="r",
                    commit_sha="abc",
                    dest_dir=tmp_path,
                    client=client,
                )

    @pytest.mark.asyncio
    async def test_server_unreachable_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ChangesetFetchError, match="server unreachable"):
                await fetcher.download(
                    repository_id="r",
                    commit_sha="abc",
                    dest_dir=tmp_path,
                    client=client,
                )

    @pytest.mark.asyncio
    async def test_request_includes_bearer_token(self, tmp_path):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/codeindex/changeset-url"):
                seen["auth"] = request.headers.get("Authorization")
                seen["body"] = json.loads(request.content)
                return httpx.Response(
                    200,
                    content=json.dumps(
                        {"signed_url": SIGNED_URL, "expires_at": "2026-04-28T00:10:00Z"}
                    ).encode(),
                    request=request,
                )
            return httpx.Response(200, content=b"x", request=request)

        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok-xyz")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await fetcher.download(
                repository_id="r-7",
                commit_sha="abc1234",
                dest_dir=tmp_path,
                client=client,
            )
        assert seen["auth"] == "Bearer tok-xyz"
        assert seen["body"] == {"repository_id": "r-7", "commit_sha": "abc1234"}


class TestDownloadSnapshot:
    @pytest.mark.asyncio
    async def test_full_signed_url_flow_hits_snapshot_endpoint(self, tmp_path):
        body = b'{"op":"commit","sha":"abc","parent_sha":null}\n'
        seen_path: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "/codeindex/" in request.url.path:
                seen_path["endpoint"] = request.url.path
                return httpx.Response(
                    200,
                    content=json.dumps(
                        {"signed_url": SIGNED_URL, "expires_at": "2026-04-28T00:10:00Z"}
                    ).encode(),
                    request=request,
                )
            if str(request.url) == SIGNED_URL:
                return httpx.Response(200, content=body, request=request)
            return httpx.Response(500, content=b"unexpected", request=request)

        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            target = await fetcher.download_snapshot(
                repository_id="r",
                commit_sha="abc",
                dest_dir=tmp_path,
                client=client,
            )
        assert target == tmp_path / "abc.jsonl"
        assert target.read_bytes() == body
        # Pin the actual endpoint suffix so a typo or accidental fallthrough
        # to /changeset-url would surface here.
        assert seen_path["endpoint"].endswith("/codeindex/changeset-snapshot")

    @pytest.mark.asyncio
    async def test_403_from_snapshot_endpoint_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/codeindex/changeset-snapshot"):
                return httpx.Response(403, content=b'{"detail":"no access"}', request=request)
            return httpx.Response(500, request=request)

        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ChangesetFetchError, match="access denied"):
                await fetcher.download_snapshot(
                    repository_id="r",
                    commit_sha="abc",
                    dest_dir=tmp_path,
                    client=client,
                )


class TestPullAndApply:
    @pytest.mark.asyncio
    async def test_calls_apply_delta_with_downloaded_file(self, tmp_path, monkeypatch):
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")

        canned = tmp_path / "abc.jsonl"
        canned.write_text("{}")
        fetcher.download = AsyncMock(return_value=canned)  # type: ignore[method-assign]

        captured: dict = {}

        async def fake_apply(*, index, file_refs, jsonl_path):
            captured["jsonl_path"] = Path(jsonl_path)
            captured["index"] = index
            captured["file_refs"] = file_refs
            from ember_code.core.code_index.delta import DeltaStats

            return DeltaStats(items_upserted=3)

        monkeypatch.setattr("ember_code.core.code_index.fetcher.apply_delta", fake_apply)

        index = object()
        file_refs = object()
        stats = await fetcher.pull_and_apply(
            index=index,
            file_refs=file_refs,
            repository_id="r",
            commit_sha="abc",
        )
        assert stats.items_upserted == 3
        assert captured["index"] is index
        assert captured["file_refs"] is file_refs
        assert captured["jsonl_path"] == canned

    @pytest.mark.asyncio
    async def test_pull_and_apply_snapshot_uses_snapshot_downloader(self, tmp_path, monkeypatch):
        """``pull_and_apply_snapshot`` must download via the snapshot
        endpoint, not the per-commit-delta one."""
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")

        canned = tmp_path / "abc.jsonl"
        canned.write_text("{}")
        snapshot_called = AsyncMock(return_value=canned)
        delta_called = AsyncMock(return_value=canned)
        fetcher.download_snapshot = snapshot_called  # type: ignore[method-assign]
        fetcher.download = delta_called  # type: ignore[method-assign]

        async def fake_apply(*, index, file_refs, jsonl_path):
            from ember_code.core.code_index.delta import DeltaStats

            return DeltaStats(items_upserted=1)

        monkeypatch.setattr("ember_code.core.code_index.fetcher.apply_delta", fake_apply)

        await fetcher.pull_and_apply_snapshot(
            index=object(),
            file_refs=object(),
            repository_id="r",
            commit_sha="abc",
        )
        snapshot_called.assert_awaited_once()
        delta_called.assert_not_called()


class TestPreflight:
    @pytest.mark.asyncio
    async def test_returns_parsed_ok_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/codeindex/preflight")
            assert json.loads(request.content) == {"repository_id": "r", "commit_sha": "abc"}
            assert request.headers["authorization"] == "Bearer tok"
            return httpx.Response(200, json={"status": "ok"})

        transport = httpx.MockTransport(handler)
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            result = await fetcher.preflight(repository_id="r", commit_sha="abc", client=client)
        assert result.status == PreflightStatus.OK

    @pytest.mark.asyncio
    async def test_parses_in_progress_with_progress_fields(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "in_progress",
                    "progress_percentage": 60,
                    "current_step": "Phase 4: Reference resolution",
                    "started_at": "2026-04-29T12:00:00Z",
                },
            )

        transport = httpx.MockTransport(handler)
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            result = await fetcher.preflight(repository_id="r", commit_sha="abc", client=client)
        assert result.status == PreflightStatus.IN_PROGRESS
        assert result.progress_percentage == 60
        assert result.current_step == "Phase 4: Reference resolution"
        assert result.started_at is not None

    @pytest.mark.asyncio
    async def test_parses_link_required_with_link_url(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "link_required", "link_start_url": "/v1/auth/github/link/start"},
            )

        transport = httpx.MockTransport(handler)
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            result = await fetcher.preflight(repository_id="r", commit_sha="abc", client=client)
        assert result.status == PreflightStatus.LINK_REQUIRED
        assert result.link_start_url == "/v1/auth/github/link/start"

    @pytest.mark.asyncio
    async def test_401_raises_changeset_fetch_error(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"not authorized")

        transport = httpx.MockTransport(handler)
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ChangesetFetchError, match="unauthorized"):
                await fetcher.preflight(repository_id="r", commit_sha="abc", client=client)

    @pytest.mark.asyncio
    async def test_malformed_payload_raises(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "totally-bogus"})

        transport = httpx.MockTransport(handler)
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ChangesetFetchError, match="malformed"):
                await fetcher.preflight(repository_id="r", commit_sha="abc", client=client)

    @pytest.mark.asyncio
    async def test_parses_parent_sha_from_ok_response(self):
        """``parent_sha`` must round-trip from the server's OK response —
        the sync manager uses it to decide between the delta and
        snapshot endpoints."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok", "parent_sha": "deadbeef" * 5})

        transport = httpx.MockTransport(handler)
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            result = await fetcher.preflight(repository_id="r", commit_sha="abc", client=client)
        assert result.status == PreflightStatus.OK
        assert result.parent_sha == "deadbeef" * 5

    @pytest.mark.asyncio
    async def test_parent_sha_null_for_root_commit(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok", "parent_sha": None})

        transport = httpx.MockTransport(handler)
        fetcher = ChangesetFetcher(server_url="http://srv", bearer_token="tok")
        async with httpx.AsyncClient(transport=transport) as client:
            result = await fetcher.preflight(repository_id="r", commit_sha="abc", client=client)
        assert result.parent_sha is None
