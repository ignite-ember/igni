"""Tests for CodeIndexSyncManager — orchestration, skip paths, watcher."""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.code_index.delta import DeltaStats
from ember_code.core.code_index.fetcher import (
    ChangesetFetchError,
    PreflightResult,
    PreflightStatus,
)
from ember_code.core.code_index.resolver import DiscoveryStatus, ResolvedRepository
from ember_code.core.code_index.sync_manager import (
    CodeIndexSyncManager,
    SyncResult,
)
from ember_code.core.config.settings import Settings


def _patch_preflight(monkeypatch, result: PreflightResult) -> None:
    """Default helper: short-circuit ChangesetFetcher.preflight to return ``result``."""
    from ember_code.core.code_index import sync_manager as sm

    async def _fake(self, *, repository_id, commit_sha, client=None):
        return result

    monkeypatch.setattr(sm.ChangesetFetcher, "preflight", _fake)


_OK = PreflightResult(status=PreflightStatus.OK)


def _stub_resolver(resolved: ResolvedRepository | None = None):
    resolver = MagicMock()
    resolver.cached = resolved
    resolver.remote_url = MagicMock(return_value="https://github.com/acme/widgets")
    resolver.resolve = AsyncMock(return_value=resolved)
    return resolver


def _stub_index():
    index = MagicMock()
    index._file_reference_service = MagicMock(return_value=MagicMock())
    return index


def _stub_credentials(token: str | None = "tok-xyz"):
    creds = MagicMock()
    creds.access_token = token
    return creds


def _make_mgr(
    *,
    project_dir,
    code_index=None,
    resolver=None,
    credentials=None,
    server_url="http://srv",
):
    return CodeIndexSyncManager(
        project_dir=project_dir,
        code_index=code_index,
        resolver=resolver,
        credentials=credentials,
        server_url=server_url,
    )


_RESOLVED = ResolvedRepository(
    status=DiscoveryStatus.REGISTERED,
    repository_id="repo-uuid-1",
)
_NEEDS_INSTALL = ResolvedRepository(
    status=DiscoveryStatus.INSTALL_REQUIRED,
    install_url="https://github.com/apps/ember-codeindex/installations/new?state=...",
)


class TestSyncResult:
    def test_succeeded_when_stats_present(self):
        assert SyncResult(stats=DeltaStats(), commit_sha="abc").succeeded is True

    def test_not_succeeded_when_skipped(self):
        assert SyncResult(skipped=True, reason="x").succeeded is False

    def test_not_succeeded_when_error(self):
        assert SyncResult(error="boom").succeeded is False


class TestSyncSkipPaths:
    @pytest.mark.asyncio
    async def test_skips_when_no_code_index(self, tmp_path):
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=None,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        result = await mgr.sync_now(sha="abc")
        assert result.skipped and "code index" in result.reason

    @pytest.mark.asyncio
    async def test_skips_when_not_a_git_repo(self, tmp_path):
        mgr = _make_mgr(
            project_dir=tmp_path,  # not a git repo
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        result = await mgr.sync_now()
        assert result.skipped and "git" in result.reason

    @pytest.mark.asyncio
    async def test_skips_when_resolver_returns_none(self, tmp_path):
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(None),
            credentials=_stub_credentials(),
        )
        result = await mgr.sync_now(sha="abc")
        assert result.skipped and "unavailable" in result.reason

    @pytest.mark.asyncio
    async def test_skips_when_no_cloud_token(self, tmp_path):
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(token=None),
        )
        result = await mgr.sync_now(sha="abc")
        assert result.skipped and "not authenticated" in result.reason

    @pytest.mark.asyncio
    async def test_install_required_surfaces_install_url(self, tmp_path):
        """When the resolver returns INSTALL_REQUIRED, sync surfaces the URL."""
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_NEEDS_INSTALL),
            credentials=_stub_credentials(),
        )
        result = await mgr.sync_now(sha="abc")
        assert result.skipped is True
        assert result.link_start_url == _NEEDS_INSTALL.install_url
        assert "install" in result.reason.lower()
        assert mgr.last_synced_sha is None


class TestSyncSuccessAndErrors:
    @pytest.mark.asyncio
    async def test_successful_sync_records_sha(self, tmp_path, monkeypatch):
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, _OK)
        captured = {}

        async def fake_pull_and_apply(
            self, *, index, file_refs, repository_id, commit_sha, on_progress=None
        ):
            captured["repository_id"] = repository_id
            captured["commit_sha"] = commit_sha
            captured["bearer_token"] = self.bearer_token
            captured["server_url"] = self.server_url
            return DeltaStats(items_upserted=5, references_upserted=3)

        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", fake_pull_and_apply)

        result = await mgr.sync_now(sha="abc1234")
        assert result.succeeded
        assert result.commit_sha == "abc1234"
        assert result.stats.items_upserted == 5
        assert mgr.last_synced_sha == "abc1234"
        assert captured["repository_id"] == "repo-uuid-1"
        assert captured["commit_sha"] == "abc1234"
        assert captured["bearer_token"] == "tok-xyz"
        assert captured["server_url"] == "http://srv"

    @pytest.mark.asyncio
    async def test_fetch_error_returned_as_sync_error(self, tmp_path, monkeypatch):
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, _OK)

        async def boom(self, **_kwargs):
            raise ChangesetFetchError("access denied")

        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", boom)

        result = await mgr.sync_now(sha="abc")
        assert not result.succeeded
        assert "access denied" in (result.error or "")
        assert mgr.last_synced_sha is None

    @pytest.mark.asyncio
    async def test_unexpected_error_returned_as_sync_error(self, tmp_path, monkeypatch):
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, _OK)

        async def kaboom(self, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", kaboom)

        result = await mgr.sync_now(sha="abc")
        assert "boom" in (result.error or "")


class TestSnapshotVsDeltaRouting:
    """Pins the routing between the delta and snapshot endpoints.

    The rule: pick the endpoint that keeps the local chroma matching the
    cloud's definition. Delta is safe only when there's a local ancestor
    to copy-on-write from; otherwise the only correct choice is the
    snapshot. Branches we pin:

    * No usable local ancestor (root or missing parent) → snapshot.
    * Parent present locally → delta + copy-on-write.
    * Target already indexed locally → delta (idempotent replay).
    * ``force_snapshot=True`` → snapshot regardless of state.
    """

    @pytest.mark.asyncio
    async def test_no_parent_no_local_target_uses_snapshot(self, tmp_path, monkeypatch):
        """``parent_sha=None`` + target not local → snapshot.

        The server returns ``parent_sha=None`` whenever it lacks parent
        lineage on the preflight response, *including* for non-root
        commits. Trusting the delta here used to leave the local index
        with only the diff's items. The conservative rule below picks
        the snapshot endpoint so the index starts from a known full
        state.
        """
        index = _stub_index()
        index.has_commit = MagicMock(return_value=False)
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=index,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, PreflightResult(status=PreflightStatus.OK, parent_sha=None))
        delta_called = AsyncMock(return_value=DeltaStats(items_upserted=1))
        snapshot_called = AsyncMock(return_value=DeltaStats(items_upserted=99))
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", delta_called)
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply_snapshot", snapshot_called)

        result = await mgr.sync_now(sha="abc1234")
        assert result.succeeded
        snapshot_called.assert_awaited_once()
        delta_called.assert_not_called()
        assert result.stats.items_upserted == 99

    @pytest.mark.asyncio
    async def test_target_already_local_uses_delta(self, tmp_path, monkeypatch):
        """Target chroma already exists locally → delta endpoint.

        Re-applying the JSONL on top of an existing chroma is the
        idempotent replay path; there's no need to pay the cost of a
        full snapshot when the local state is already populated.
        """
        index = _stub_index()
        # has_commit("abc1234") returns True (target present); anything
        # else returns False.
        index.has_commit = MagicMock(side_effect=lambda sha: sha == "abc1234")
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=index,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, PreflightResult(status=PreflightStatus.OK, parent_sha=None))
        delta_called = AsyncMock(return_value=DeltaStats(items_upserted=1))
        snapshot_called = AsyncMock(return_value=DeltaStats(items_upserted=99))
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", delta_called)
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply_snapshot", snapshot_called)

        result = await mgr.sync_now(sha="abc1234")
        assert result.succeeded
        delta_called.assert_awaited_once()
        snapshot_called.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_progress_callback_is_wired(self, tmp_path, monkeypatch):
        """The SyncManager must hand its ``_on_apply_progress`` callback
        to ``apply_delta`` through the fetcher — otherwise the apply
        runs but ``_apply_done`` / ``_apply_total`` stay at 0 and the
        TUI's ``Resyncing N/M`` busy label never updates.

        Capturing the kwarg from the stub is the only way to confirm
        the wiring; ``AsyncMock`` silently accepts any kwargs, so
        removing ``on_progress=...`` from sync_manager.py would
        otherwise pass every existing routing test.
        """
        index = _stub_index()
        index.has_commit = MagicMock(return_value=False)
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=index,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, PreflightResult(status=PreflightStatus.OK, parent_sha=None))
        snapshot_called = AsyncMock(return_value=DeltaStats(items_upserted=1))
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", AsyncMock())
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply_snapshot", snapshot_called)

        await mgr.sync_now(sha="abc1234")

        snapshot_called.assert_awaited_once()
        kwargs = snapshot_called.call_args.kwargs
        assert "on_progress" in kwargs, "fetcher must receive on_progress kwarg"
        # Bound methods compare equal but each access creates a fresh
        # wrapper object, so ``is`` doesn't hold. Verify identity via
        # behaviour: invoking the captured callback must mutate this
        # manager's progress fields.
        cb = kwargs["on_progress"]
        assert callable(cb), "on_progress must be a callable"
        mgr._apply_done = 0
        mgr._apply_total = 0
        mgr._apply_step = ""
        cb(7, 28, "math_utils.py")
        assert (mgr._apply_done, mgr._apply_total, mgr._apply_step) == (7, 28, "math_utils.py"), (
            "callback must update the SyncManager's own progress state"
        )

    @pytest.mark.asyncio
    async def test_applying_flag_and_progress_lifecycle(self, tmp_path, monkeypatch):
        """While ``apply_delta`` is running, ``_applying`` must be True
        and the live progress fields must reflect callback values.
        After the call returns (success OR failure), ``_applying``
        flips back to False via the ``finally`` clause — otherwise the
        TUI's status poll would report a stale "syncing" state forever.
        """
        index = _stub_index()
        index.has_commit = MagicMock(return_value=False)
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=index,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, PreflightResult(status=PreflightStatus.OK, parent_sha=None))

        snapshots: list[tuple[bool, int, int, str]] = []

        async def fake_snapshot(self, *, index, file_refs, repository_id, commit_sha, on_progress):
            # Simulate what apply_delta does: invoke the callback at
            # several points; record the manager's state at each so
            # the test can assert the live update.
            snapshots.append((mgr._applying, mgr._apply_done, mgr._apply_total, mgr._apply_step))
            on_progress(0, 28, "preparing")
            snapshots.append((mgr._applying, mgr._apply_done, mgr._apply_total, mgr._apply_step))
            on_progress(14, 28, "math_utils.py::evaluate")
            snapshots.append((mgr._applying, mgr._apply_done, mgr._apply_total, mgr._apply_step))
            on_progress(28, 28, "security_helpers.py::run_shell")
            snapshots.append((mgr._applying, mgr._apply_done, mgr._apply_total, mgr._apply_step))
            return DeltaStats(items_upserted=28)

        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply_snapshot", fake_snapshot)

        result = await mgr.sync_now(sha="abc1234", force_snapshot=True)

        assert result.succeeded
        # Snapshot just before the first callback — _applying flipped
        # True, counters reset to 0.
        assert snapshots[0] == (True, 0, 0, "")
        # After "preparing" with total=28.
        assert snapshots[1] == (True, 0, 28, "preparing")
        # Mid-apply: 14/28, current item path.
        assert snapshots[2] == (True, 14, 28, "math_utils.py::evaluate")
        # Final callback before fetcher returns.
        assert snapshots[3] == (True, 28, 28, "security_helpers.py::run_shell")
        # After the call returns, the finally clause flips _applying
        # back to False even though the counters still reflect the
        # last state — that's fine because the TUI gates its
        # "syncing" display on _applying, not the counters.
        assert mgr._applying is False
        assert mgr._apply_done == 28
        assert mgr._apply_total == 28

    @pytest.mark.asyncio
    async def test_applying_flag_clears_on_error(self, tmp_path, monkeypatch):
        """If the fetcher raises (network failure, malformed JSONL),
        ``_applying`` must still flip back to False. Without the
        ``finally`` clause an apply that crashed mid-flight would
        leave the panel stuck on "syncing" forever."""
        index = _stub_index()
        index.has_commit = MagicMock(return_value=False)
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=index,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, PreflightResult(status=PreflightStatus.OK, parent_sha=None))

        async def boom(self, **_kwargs):
            raise ChangesetFetchError("network exploded")

        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply_snapshot", boom)

        result = await mgr.sync_now(sha="abc1234", force_snapshot=True)

        assert not result.succeeded
        assert mgr._applying is False

    @pytest.mark.asyncio
    async def test_force_snapshot_overrides_routing(self, tmp_path, monkeypatch):
        """``force_snapshot=True`` ignores parent / target presence."""
        index = _stub_index()
        # Both target and parent present — would normally pick delta.
        index.has_commit = MagicMock(return_value=True)
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=index,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(
            monkeypatch,
            PreflightResult(status=PreflightStatus.OK, parent_sha="parentsha"),
        )
        delta_called = AsyncMock(return_value=DeltaStats(items_upserted=1))
        snapshot_called = AsyncMock(return_value=DeltaStats(items_upserted=99))
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", delta_called)
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply_snapshot", snapshot_called)

        result = await mgr.sync_now(sha="abc1234", force_snapshot=True)
        assert result.succeeded
        snapshot_called.assert_awaited_once()
        delta_called.assert_not_called()

    @pytest.mark.asyncio
    async def test_parent_present_locally_uses_delta_endpoint(self, tmp_path, monkeypatch):
        index = _stub_index()
        index.has_commit = MagicMock(return_value=True)
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=index,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(
            monkeypatch,
            PreflightResult(status=PreflightStatus.OK, parent_sha="parentsha"),
        )
        delta_called = AsyncMock(return_value=DeltaStats(items_upserted=1))
        snapshot_called = AsyncMock(return_value=DeltaStats(items_upserted=99))
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", delta_called)
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply_snapshot", snapshot_called)

        result = await mgr.sync_now(sha="abc1234")
        assert result.succeeded
        delta_called.assert_awaited_once()
        snapshot_called.assert_not_called()
        # New routing also peeks at target_sha to allow idempotent
        # replay when the chroma is already there. Both checks must
        # happen; the parent check is the load-bearing one.
        index.has_commit.assert_any_call("parentsha")

    @pytest.mark.asyncio
    async def test_parent_missing_locally_uses_snapshot_endpoint(self, tmp_path, monkeypatch):
        index = _stub_index()
        index.has_commit = MagicMock(return_value=False)
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=index,
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(
            monkeypatch,
            PreflightResult(status=PreflightStatus.OK, parent_sha="parentsha"),
        )
        delta_called = AsyncMock(return_value=DeltaStats(items_upserted=1))
        snapshot_called = AsyncMock(return_value=DeltaStats(items_upserted=99))
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", delta_called)
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply_snapshot", snapshot_called)

        result = await mgr.sync_now(sha="abc1234")
        assert result.succeeded
        snapshot_called.assert_awaited_once()
        delta_called.assert_not_called()
        assert result.stats.items_upserted == 99


class TestCurrentSha:
    def test_returns_none_when_not_a_git_repo(self, tmp_path):
        mgr = _make_mgr(project_dir=tmp_path)
        assert mgr.current_sha() is None

    def test_reads_head_from_real_git_repo(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("hi")
        subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

        mgr = _make_mgr(project_dir=tmp_path)
        sha = mgr.current_sha()
        assert sha is not None
        assert len(sha) == 40


class TestFromSettings:
    def test_builds_resolver_from_auth_server_url(self, tmp_path):
        settings = Settings()
        mgr = CodeIndexSyncManager.from_settings(
            settings, project_dir=tmp_path, code_index=_stub_index()
        )
        assert mgr.resolver is not None
        assert mgr.server_url == settings.api_url.rstrip("/")


class TestWatcher:
    @pytest.mark.asyncio
    async def test_watcher_fires_only_on_sha_change(self, tmp_path):
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        sequence = iter(["a" * 40, "a" * 40, "b" * 40, "b" * 40])
        mgr.current_sha = lambda: next(sequence, None)
        mgr.sync_now = AsyncMock(side_effect=lambda sha=None: SyncResult(commit_sha=sha))

        await mgr.start_watcher(interval_seconds=0.01)
        await asyncio.sleep(0.06)
        await mgr.stop_watcher()

        called_shas = [call.kwargs["sha"] for call in mgr.sync_now.await_args_list]
        unique_shas = sorted(set(called_shas))
        assert unique_shas == ["a" * 40, "b" * 40]

    @pytest.mark.asyncio
    async def test_stop_watcher_is_idempotent(self, tmp_path):
        mgr = _make_mgr(project_dir=tmp_path)
        await mgr.stop_watcher()
        await mgr.stop_watcher()


class TestConcurrentSyncSerializes:
    @pytest.mark.asyncio
    async def test_overlapping_calls_dont_double_apply(self, tmp_path, monkeypatch):
        in_flight = 0
        peak = 0

        async def slow_pull_and_apply(self, **_kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return DeltaStats(items_upserted=1)

        from ember_code.core.code_index import sync_manager as sm

        _patch_preflight(monkeypatch, _OK)
        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", slow_pull_and_apply)

        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        await asyncio.gather(
            mgr.sync_now(sha="abc"),
            mgr.sync_now(sha="abc"),
            mgr.sync_now(sha="abc"),
        )
        assert peak == 1


class TestPreflightBranching:
    """Each non-OK preflight status maps to a structured SyncResult."""

    @pytest.mark.asyncio
    async def test_in_progress_returns_skipped_with_progress(self, tmp_path, monkeypatch):
        _patch_preflight(
            monkeypatch,
            PreflightResult(
                status=PreflightStatus.IN_PROGRESS,
                progress_percentage=42,
                current_step="Phase 4: Reference resolution",
            ),
        )
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        result = await mgr.sync_now(sha="abc")
        assert result.skipped is True
        assert result.in_progress is True
        assert result.preflight_status == PreflightStatus.IN_PROGRESS
        assert result.progress_percentage == 42
        assert result.current_step == "Phase 4: Reference resolution"
        # No download attempted → last_synced_sha stays None.
        assert mgr.last_synced_sha is None

    @pytest.mark.asyncio
    async def test_link_required_returns_link_url(self, tmp_path, monkeypatch):
        _patch_preflight(
            monkeypatch,
            PreflightResult(
                status=PreflightStatus.LINK_REQUIRED, link_start_url="/v1/auth/github/link/start"
            ),
        )
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        result = await mgr.sync_now(sha="abc")
        assert result.needs_link is True
        assert result.link_start_url == "/v1/auth/github/link/start"
        assert result.skipped is True

    @pytest.mark.asyncio
    async def test_no_matching_account_also_needs_link(self, tmp_path, monkeypatch):
        _patch_preflight(
            monkeypatch,
            PreflightResult(
                status=PreflightStatus.NO_MATCHING_ACCOUNT,
                link_start_url="/v1/auth/github/link/start",
            ),
        )
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        result = await mgr.sync_now(sha="abc")
        assert result.needs_link is True

    @pytest.mark.asyncio
    async def test_failed_returns_error(self, tmp_path, monkeypatch):
        _patch_preflight(
            monkeypatch,
            PreflightResult(
                status=PreflightStatus.FAILED, error_message="AST parser crashed on foo.py"
            ),
        )
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        result = await mgr.sync_now(sha="abc")
        assert result.error == "AST parser crashed on foo.py"
        assert result.preflight_status == PreflightStatus.FAILED

    @pytest.mark.asyncio
    async def test_changeset_not_found_skipped(self, tmp_path, monkeypatch):
        _patch_preflight(monkeypatch, PreflightResult(status=PreflightStatus.CHANGESET_NOT_FOUND))
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        result = await mgr.sync_now(sha="abc")
        assert result.skipped is True
        assert result.preflight_status == PreflightStatus.CHANGESET_NOT_FOUND

    @pytest.mark.asyncio
    async def test_repo_not_found_skipped(self, tmp_path, monkeypatch):
        _patch_preflight(monkeypatch, PreflightResult(status=PreflightStatus.REPO_NOT_FOUND))
        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        result = await mgr.sync_now(sha="abc")
        assert result.skipped is True
        assert result.preflight_status == PreflightStatus.REPO_NOT_FOUND

    @pytest.mark.asyncio
    async def test_non_ok_preflight_skips_download(self, tmp_path, monkeypatch):
        """If preflight isn't OK, pull_and_apply must not be called — important
        because the old code would fall through to a 403/404 from the signed-URL
        endpoint and surface a confusing error."""
        _patch_preflight(monkeypatch, PreflightResult(status=PreflightStatus.IN_PROGRESS))

        from ember_code.core.code_index import sync_manager as sm

        called = False

        async def fail_pull(*_args, **_kwargs):
            nonlocal called
            called = True
            return DeltaStats()

        monkeypatch.setattr(sm.ChangesetFetcher, "pull_and_apply", fail_pull)

        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )
        await mgr.sync_now(sha="abc")
        assert called is False


class TestWatcherInProgressRetry:
    """Verify the watcher polls a stuck IN_PROGRESS commit every 15s on the
    same sha (without HEAD changing), and stops as soon as the server flips
    to OK or any other terminal status."""

    @pytest.mark.asyncio
    async def test_in_progress_sha_polled_again_after_retry_interval(self, tmp_path, monkeypatch):
        from ember_code.core.code_index import sync_manager as sm

        # Make the retry interval tiny so the test runs in milliseconds.
        monkeypatch.setattr(sm, "IN_PROGRESS_RETRY_SECONDS", 0.05)

        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        # HEAD never changes — still 'aaaa…'
        mgr.current_sha = lambda: "a" * 40

        responses = iter(
            [
                SyncResult(commit_sha="a" * 40, preflight_status=PreflightStatus.IN_PROGRESS),
                SyncResult(commit_sha="a" * 40, preflight_status=PreflightStatus.IN_PROGRESS),
                SyncResult(
                    commit_sha="a" * 40, stats=DeltaStats(), preflight_status=PreflightStatus.OK
                ),
            ]
        )
        mgr.sync_now = AsyncMock(side_effect=lambda sha=None: next(responses))

        await mgr.start_watcher(interval_seconds=0.01)
        await asyncio.sleep(0.25)
        await mgr.stop_watcher()

        # Multiple sync_now calls for the same sha = retry worked.
        called_shas = [c.kwargs["sha"] for c in mgr.sync_now.await_args_list]
        assert len(called_shas) >= 3
        assert all(s == "a" * 40 for s in called_shas)

    @pytest.mark.asyncio
    async def test_in_progress_state_cleared_when_head_moves(self, tmp_path, monkeypatch):
        """If the user switches branches mid-poll, drop the stale retry state."""
        from ember_code.core.code_index import sync_manager as sm

        monkeypatch.setattr(sm, "IN_PROGRESS_RETRY_SECONDS", 5.0)  # never fires in this test

        mgr = _make_mgr(
            project_dir=tmp_path,
            code_index=_stub_index(),
            resolver=_stub_resolver(_RESOLVED),
            credentials=_stub_credentials(),
        )

        sequence = iter(["a" * 40, "a" * 40, "b" * 40, "b" * 40])
        mgr.current_sha = lambda: next(sequence, None)

        responses = iter(
            [
                SyncResult(commit_sha="a" * 40, preflight_status=PreflightStatus.IN_PROGRESS),
                SyncResult(
                    commit_sha="b" * 40, stats=DeltaStats(), preflight_status=PreflightStatus.OK
                ),
            ]
        )
        mgr.sync_now = AsyncMock(side_effect=lambda sha=None: next(responses))

        await mgr.start_watcher(interval_seconds=0.01)
        await asyncio.sleep(0.06)
        await mgr.stop_watcher()

        called_shas = [c.kwargs["sha"] for c in mgr.sync_now.await_args_list]
        # Both shas were synced; the in_progress state for 'a' didn't block 'b'.
        assert "a" * 40 in called_shas
        assert "b" * 40 in called_shas
        # And after a successful 'b', the in_progress retry state should be clear.
        assert mgr._in_progress_sha is None
