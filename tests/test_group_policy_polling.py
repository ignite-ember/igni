"""The timer that re-asks, and the 304 that makes asking cheap.

A session used to learn its group once, at start. It now re-checks on a
timer — which is only defensible because the usual answer costs nothing:
the client sends the tag it holds and the server says "unchanged" with
no body.

Both halves need to hold or the feature is worse than not having it. A
loop that dies on the first error stops silently, which is worse than
never polling. And an "unchanged" that the cache mistakes for "no group"
would throw away a working configuration on every poll.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.server_auth import AuthController
from ember_code.core.config.group_policy import (
    PACK_UNCHANGED,
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyPack,
)


def _pack(names: list[str], etag: str | None = None) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        etag=etag,
        entries=[
            GroupPolicyEntry(
                kind="agents",
                entry_name=n,
                content=f"---\nname: {n}\n---\nBody.",
                content_type="markdown",
            )
            for n in names
        ],
    )


def _controller(tmp_path: Path, poll_seconds: int = 300) -> AuthController:
    ctrl = AuthController.__new__(AuthController)
    ctrl._settings = SimpleNamespace(
        api_url="https://api.example.invalid",
        storage=SimpleNamespace(data_dir=str(tmp_path)),
        auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json"), access_token="t"),
        group_policy=SimpleNamespace(poll_seconds=poll_seconds),
    )
    ctrl._portal = MagicMock()
    ctrl._hydration_lock = None
    ctrl._status_provider = MagicMock()
    ctrl._session = MagicMock()
    ctrl._poll_task = None
    return ctrl


class TestTheTimer:
    @pytest.mark.asyncio
    async def test_it_starts(self, tmp_path: Path):
        ctrl = _controller(tmp_path)
        ctrl.start_group_policy_polling()
        try:
            assert ctrl._poll_task is not None
        finally:
            ctrl.stop_group_policy_polling()

    @pytest.mark.asyncio
    async def test_zero_turns_it_off(self, tmp_path: Path):
        """For a deployment that would rather its machines only check at
        startup."""
        ctrl = _controller(tmp_path, poll_seconds=0)

        ctrl.start_group_policy_polling()

        assert ctrl._poll_task is None

    @pytest.mark.asyncio
    async def test_starting_twice_does_not_make_two(self, tmp_path: Path):
        ctrl = _controller(tmp_path)
        ctrl.start_group_policy_polling()
        first = ctrl._poll_task
        try:
            ctrl.start_group_policy_polling()
            assert ctrl._poll_task is first
        finally:
            ctrl.stop_group_policy_polling()

    @pytest.mark.asyncio
    async def test_stopping_is_safe_when_it_never_started(self, tmp_path: Path):
        _controller(tmp_path).stop_group_policy_polling()

    @pytest.mark.asyncio
    async def test_it_actually_hydrates(self, tmp_path: Path):
        ctrl = _controller(tmp_path)
        ctrl._hydrate_group_policy = AsyncMock(return_value=True)

        task = asyncio.get_running_loop().create_task(ctrl._poll_group_policy(0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()

        ctrl._hydrate_group_policy.assert_awaited()

    @pytest.mark.asyncio
    async def test_an_error_does_not_stop_it(self, tmp_path: Path):
        """A loop that dies on the first failure stops silently, which
        is worse than never polling: nothing says it went away."""
        ctrl = _controller(tmp_path)
        calls = []

        async def flaky(_token):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("server restarting")
            return True

        ctrl._hydrate_group_policy = flaky
        task = asyncio.get_running_loop().create_task(ctrl._poll_group_policy(0))
        for _ in range(8):
            await asyncio.sleep(0)
        task.cancel()

        assert len(calls) > 1

    @pytest.mark.asyncio
    async def test_cancelling_ends_it(self, tmp_path: Path):
        ctrl = _controller(tmp_path)
        ctrl._hydrate_group_policy = AsyncMock(return_value=False)

        task = asyncio.get_running_loop().create_task(ctrl._poll_group_policy(0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestAnUnchangedPack:
    """What a 304 has to mean to the cache."""

    @pytest.mark.asyncio
    async def test_nothing_is_rewritten(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path / "gp")
        cache.materialize(_pack(["reviewer", "debugger"]))
        before = sorted(p.name for p in cache.agents_dir.glob("*.md"))

        refreshed = await cache.refresh_if_stale("t", AsyncMock(return_value=PACK_UNCHANGED))

        assert refreshed is False
        assert sorted(p.name for p in cache.agents_dir.glob("*.md")) == before

    @pytest.mark.asyncio
    async def test_it_is_not_mistaken_for_no_group(self, tmp_path: Path):
        """``None`` means "I could not tell you" and leaves the cache
        alone too — but only ``PACK_UNCHANGED`` should mark it fresh."""
        cache = GroupPolicyCache(cache_dir=tmp_path / "gp")
        cache.materialize(_pack(["reviewer"]))
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        meta_path = cache._cache_dir / "pack_meta.json"
        meta = json.loads(meta_path.read_text())
        meta["fetched_at"] = stale
        meta_path.write_text(json.dumps(meta))

        await cache.refresh_if_stale("t", AsyncMock(return_value=PACK_UNCHANGED))

        assert json.loads(meta_path.read_text())["fetched_at"] != stale

    @pytest.mark.asyncio
    async def test_a_confirmed_pack_stops_being_stale(self, tmp_path: Path):
        """Otherwise every poll would find it stale and ask again, and
        the 304 would save nothing."""
        cache = GroupPolicyCache(cache_dir=tmp_path / "gp")
        cache.materialize(_pack(["reviewer"]))
        meta_path = cache._cache_dir / "pack_meta.json"
        meta = json.loads(meta_path.read_text())
        meta["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        meta_path.write_text(json.dumps(meta))

        fetch = AsyncMock(return_value=PACK_UNCHANGED)
        await cache.refresh_if_stale("t", fetch)
        await cache.refresh_if_stale("t", fetch)

        # The second call found it fresh and never asked.
        assert fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_the_tag_is_handed_to_the_fetcher(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path / "gp")
        cache.materialize(_pack(["reviewer"], etag='"abc"'))
        meta_path = cache._cache_dir / "pack_meta.json"
        meta = json.loads(meta_path.read_text())
        meta["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        meta_path.write_text(json.dumps(meta))

        fetch = AsyncMock(return_value=PACK_UNCHANGED)
        await cache.refresh_if_stale("t", fetch)

        fetch.assert_awaited_once_with("t", '"abc"')

    @pytest.mark.asyncio
    async def test_a_fetcher_that_takes_no_tag_still_works(self, tmp_path: Path):
        """Tests and older callers pass a one-argument coroutine."""
        cache = GroupPolicyCache(cache_dir=tmp_path / "gp")
        seen = []

        async def one_arg(token):
            seen.append(token)
            return _pack(["reviewer"])

        assert await cache.refresh_if_stale("t", one_arg) is True
        assert seen == ["t"]

    def test_the_tag_survives_materialisation(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path / "gp")
        cache.materialize(_pack(["reviewer"], etag='"abc"'))

        assert cache.stored_etag() == '"abc"'

    def test_no_tag_yet_is_not_an_error(self, tmp_path: Path):
        assert GroupPolicyCache(cache_dir=tmp_path / "gp").stored_etag() is None
