"""Tests for ``BackendServer.codeindex_status``.

Focuses on the apply-progress branch: while ``CodeIndexSyncManager``
is mid-apply (``_applying=True``), ``codeindex_status`` must report
``sync_in_progress=True`` and surface the live ``apply_done`` /
``apply_total`` / ``apply_step`` so the TUI's resync busy-label
poll can render ``Resyncing N/M · current_item``.

We bypass ``Session.__init__`` (heavy — DB, MCP, pools) and assemble
just the attributes ``codeindex_status`` reads. The method is pure
dict-shape, so this stays a unit test even though the result feeds
the on-disk panel render path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ember_code.backend.server import BackendServer
from ember_code.core.code_index.fetcher import PreflightStatus
from ember_code.core.code_index.manifest import ManifestState
from ember_code.core.code_index.resolver import DiscoveryStatus, ResolvedRepository
from ember_code.core.code_index.sync_manager import SyncResult


def _make_backend(*, sync: MagicMock, code_index: MagicMock) -> BackendServer:
    """Build a ``BackendServer`` with just enough Session machinery for
    ``codeindex_status`` to read. Bypasses ``__init__``."""
    server = BackendServer.__new__(BackendServer)
    session = MagicMock()
    session.code_index_sync = sync
    session.code_index = code_index
    server._session = session
    return server


def _stub_sync(
    *,
    applying: bool = False,
    apply_done: int = 0,
    apply_total: int = 0,
    apply_step: str = "",
    in_progress_sha: str | None = None,
    last_synced_sha: str = "",
    current_sha: str = "abc1234",
) -> MagicMock:
    sync = MagicMock()
    sync._applying = applying
    sync._apply_done = apply_done
    sync._apply_total = apply_total
    sync._apply_step = apply_step
    sync._in_progress_sha = in_progress_sha
    sync._last_sync_result = None
    sync.last_synced_sha = last_synced_sha
    sync.current_sha.return_value = current_sha
    sync.resolver = MagicMock()
    sync.resolver.cached = ResolvedRepository(
        status=DiscoveryStatus.REGISTERED, repository_id="repo-uuid"
    )
    sync.resolver.remote_url.return_value = "https://github.com/acme/repo"
    # Empty activity list so the typed ``CodeIndexStatus`` doesn't
    # try to coerce a MagicMock into ``last_sync_at: str``.
    sync.recent_activity.return_value = []
    return sync


def _stub_index(*, head: str = "abc1234") -> MagicMock:
    code_index = MagicMock()
    code_index.manifest.load.return_value = ManifestState(head=head, commits={})
    return code_index


class TestCodeIndexStatusApplyProgress:
    async def test_reports_sync_in_progress_while_applying(self):
        """Even with no server-side IN_PROGRESS preflight, a local
        apply running counts as "syncing" — the panel needs to know
        the operation isn't idle so it shows the yellow spinner."""
        sync = _stub_sync(applying=True, apply_done=10, apply_total=28)
        backend = _make_backend(sync=sync, code_index=_stub_index())

        status = await backend.codeindex_status()

        assert status.sync_in_progress is True

    async def test_progress_pct_derived_from_apply_counters(self):
        """``sync_progress_pct`` is computed live from
        ``apply_done / apply_total * 100``. The TUI's busy label
        renders this as the headline percent."""
        sync = _stub_sync(
            applying=True,
            apply_done=14,
            apply_total=28,
            apply_step="math_utils.py::evaluate",
        )
        backend = _make_backend(sync=sync, code_index=_stub_index())

        status = await backend.codeindex_status()

        assert status.sync_progress_pct == 50
        assert status.sync_step == "math_utils.py::evaluate"

    async def test_apply_counters_passed_through_raw(self):
        """The TUI also reads the raw N/M so it can render
        ``Resyncing 50% — 14/28 items · current_item`` without
        having to back-compute denominators from a percent."""
        sync = _stub_sync(
            applying=True,
            apply_done=14,
            apply_total=28,
            apply_step="security_helpers.py",
        )
        backend = _make_backend(sync=sync, code_index=_stub_index())

        status = await backend.codeindex_status()

        assert status.apply_done == 14
        assert status.apply_total == 28
        assert status.apply_step == "security_helpers.py"

    async def test_apply_fields_zeroed_when_not_applying(self):
        """After ``_applying`` flips back to False, the apply fields
        zero out even if the counters still hold the final values.
        Without this zeroing a TUI poll arriving right after a sync
        completes would briefly render "100% — 28/28" instead of
        the success message."""
        sync = _stub_sync(applying=False, apply_done=28, apply_total=28, apply_step="leftover")
        backend = _make_backend(sync=sync, code_index=_stub_index())

        status = await backend.codeindex_status()

        assert status.sync_in_progress is False
        assert status.apply_done == 0
        assert status.apply_total == 0
        assert status.apply_step == ""

    async def test_apply_progress_takes_precedence_over_stale_preflight(self):
        """When both a stale preflight in-progress result and a fresh
        local apply are present, the local apply wins — it's the
        most accurate current state."""
        sync = _stub_sync(
            applying=True,
            apply_done=7,
            apply_total=28,
            apply_step="hello.py",
            in_progress_sha="abc1234",
            current_sha="abc1234",
        )
        # Pretend the preflight had reported a different percent.
        sync._last_sync_result = SyncResult(
            commit_sha="abc1234",
            preflight_status=PreflightStatus.IN_PROGRESS,
            progress_percentage=88,
            current_step="server thinking",
        )
        backend = _make_backend(sync=sync, code_index=_stub_index())

        status = await backend.codeindex_status()

        # Local apply's 7/28 = 25%, not the preflight's stale 88%.
        assert status.sync_progress_pct == 25
        assert status.sync_step == "hello.py"

    async def test_progress_pct_uses_step_default_when_label_missing(self):
        """If ``apply_step`` is empty (callback fired before the first
        item, or simply not provided), ``sync_step`` falls back to
        ``"indexing"`` so the panel still renders something useful."""
        sync = _stub_sync(applying=True, apply_done=0, apply_total=28, apply_step="")
        backend = _make_backend(sync=sync, code_index=_stub_index())

        status = await backend.codeindex_status()

        assert status.sync_step == "indexing"

    async def test_zero_total_does_not_compute_pct(self):
        """Guard against division-by-zero: while ``_applying`` is True
        but the pre-count hasn't run yet (apply_total still 0), we
        must not produce a percent — pct stays at None / falsy."""
        sync = _stub_sync(applying=True, apply_done=0, apply_total=0, apply_step="")
        backend = _make_backend(sync=sync, code_index=_stub_index())

        status = await backend.codeindex_status()

        # sync_progress_pct stays falsy (None) — the panel hides the
        # percent slot when this is missing.
        assert not status.sync_progress_pct
