"""CodeIndex panel RPC controller.

Thin coordinator: one controller class,
:class:`CodeIndexController`, whose methods are one-to-one with
the FE-facing CodeIndex panel RPCs on
:class:`~ember_code.backend.server.BackendServer`. All wire
schemas live in :mod:`schemas_codeindex_rpc`, the head-breakdown
assembly logic lives in :mod:`head_breakdown_builder`, and the
sync-manager reach-ins previously done inline are now routed
through :meth:`CodeIndexSyncManager.progress_snapshot` +
:meth:`activity_entries`.

Wire types are re-exported here so existing imports
(``from ember_code.backend.server_codeindex import CodeIndexStatus``)
keep working — new code should import from
:mod:`schemas_codeindex_rpc` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ember_code.backend.head_breakdown_builder import (
    BranchIndexInventory,
    HeadBreakdownBuilder,
)
from ember_code.backend.schemas_codeindex_rpc import (
    BranchIndexEntry,
    CodeIndexActivityEntry,
    CodeIndexCleanResult,
    CodeIndexHeadBreakdown,
    CodeIndexInstallResult,
    CodeIndexStatus,
    CodeIndexSyncResult,
    CommitBreakdown,
    LangCount,
    LastSyncStats,
    RefreshAvailabilityResult,
)
from ember_code.core.code_index.sync.schemas import SyncProgressSnapshot

if TYPE_CHECKING:
    from ember_code.core.code_index.sync import CodeIndexSyncManager
    from ember_code.core.session import Session

__all__ = [
    "BranchIndexEntry",
    "CodeIndexActivityEntry",
    "CodeIndexCleanResult",
    "CodeIndexController",
    "CodeIndexHeadBreakdown",
    "CodeIndexInstallResult",
    "CodeIndexStatus",
    "CodeIndexSyncResult",
    "CommitBreakdown",
    "LangCount",
    "LastSyncStats",
    "RefreshAvailabilityResult",
    "SyncProgressSnapshot",
]

logger = logging.getLogger(__name__)


class CodeIndexController:
    """CodeIndex RPCs for one :class:`Session`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def status(self) -> CodeIndexStatus:
        """Status snapshot for the CodeIndex panel header."""
        sync = self._session.code_index_sync
        index = self._session.code_index
        state = index.manifest.load()
        local_sha = (await asyncio.to_thread(sync.current_sha)) or ""
        head_indexed = bool(local_sha) and local_sha in state.commits

        progress = sync.progress_snapshot()
        sync_in_progress = (
            bool(progress.in_progress_sha and progress.in_progress_sha == local_sha)
            or progress.applying
        )
        sync_progress_pct, sync_step, sync_reason, sync_error = self._derive_progress(
            progress, local_sha
        )

        install = self._resolve_install_state(sync)

        entries, index_size_bytes = BranchIndexInventory(index).build(state)

        last_sync_at, last_sync_stats = self._derive_last_sync(sync, progress)

        return CodeIndexStatus(
            local_sha=local_sha,
            remote_url=(sync.resolver.remote_url() if sync.resolver else None) or "",
            last_synced_sha=sync.last_synced_sha or "",
            index_head=state.head or "",
            head_indexed=head_indexed,
            sync_in_progress=sync_in_progress,
            sync_progress_pct=sync_progress_pct,
            sync_step=sync_step,
            sync_reason=sync_reason,
            sync_error=sync_error,
            apply_done=progress.apply_done if progress.applying else 0,
            apply_total=progress.apply_total if progress.applying else 0,
            apply_step=progress.apply_step if progress.applying else "",
            install_state=install.state,
            repository_id=install.repository_id,
            install_url=install.install_url,
            commits_indexed=len(state.commits),
            index_size_bytes=index_size_bytes,
            branches_indexed=entries,
            last_sync_at=last_sync_at,
            last_sync_stats=last_sync_stats,
        )

    async def sync(self, sha: str | None) -> CodeIndexSyncResult:
        """Pull and apply a changeset. ``sha=None`` defaults to HEAD."""
        result = await self._session.code_index_sync.sync_now(sha=sha)
        self._refresh_availability("sync")
        stats = result.stats
        return CodeIndexSyncResult(
            skipped=result.skipped,
            reason=result.reason or "",
            commit_sha=result.commit_sha or "",
            error=result.error or "",
            link_start_url=result.link_start_url or "",
            items_upserted=stats.items_upserted if stats else 0,
            items_deleted=stats.items_deleted if stats else 0,
            references_upserted=stats.references_upserted if stats else 0,
        )

    async def resync(self, sha: str | None) -> CodeIndexSyncResult:
        """Wipe the local chroma for ``sha`` and pull a fresh
        snapshot."""
        target_sha = sha or (await asyncio.to_thread(self._session.code_index_sync.current_sha))
        forgot = False
        if target_sha:
            forgot = await self._session.code_index.forget_commit(target_sha)
        result = await self._session.code_index_sync.sync_now(sha=target_sha, force_snapshot=True)
        self._refresh_availability("resync")
        stats = result.stats
        return CodeIndexSyncResult(
            forgot=forgot,
            skipped=result.skipped,
            reason=result.reason or "",
            commit_sha=result.commit_sha or "",
            error=result.error or "",
            link_start_url=result.link_start_url or "",
            items_upserted=stats.items_upserted if stats else 0,
            items_deleted=stats.items_deleted if stats else 0,
            references_upserted=stats.references_upserted if stats else 0,
        )

    async def clean(self) -> CodeIndexCleanResult:
        """Drop commits past the retention rules."""
        dropped = await self._session.code_index.clean()
        return CodeIndexCleanResult(dropped=list(dropped))

    async def head_breakdown(self) -> CodeIndexHeadBreakdown:
        """Repo-at-HEAD language histogram + recent commit list +
        per-language indexed counts.

        Delegates to :class:`HeadBreakdownBuilder` — the shell-
        outs to ``git ls-files`` / ``git log``, the extension
        counter, and the head-stats read are its concern."""
        return await HeadBreakdownBuilder(
            self._session.project_dir, self._session.code_index
        ).build()

    def activity(self) -> list[CodeIndexActivityEntry]:
        """Recent sync events for the panel's activity log."""
        return self._session.code_index_sync.activity_entries()

    def install(self) -> CodeIndexInstallResult:
        """Return the URL of the Ember portal's repositories page."""
        return CodeIndexInstallResult.from_api_url(self._session.settings.api_url)

    # ── Private helpers ──────────────────────────────────────────

    def _refresh_availability(self, verb: str) -> None:
        """Re-derive codeindex_available; log-and-swallow errors.

        Now branches on :class:`RefreshAvailabilityResult` instead
        of wrapping the call in a bare ``except Exception``. The
        Session method already catches downstream exceptions and
        packages them; we only need to check ``ok``.
        """
        refresh = self._session.refresh_codeindex_availability()
        if not refresh.ok:
            logger.debug(
                "refresh_codeindex_availability after %s failed (%s)",
                verb,
                refresh.error,
            )

    @staticmethod
    def _derive_progress(
        progress: SyncProgressSnapshot, local_sha: str
    ) -> tuple[int | None, str, str, str]:
        """Compose ``(pct, step, reason, error)`` for the wire.

        Two overlapping signals feed the panel's progress
        display: (1) the last preflight/pull result cached on
        the sync manager, and (2) the live apply-callback
        counters. The apply path wins when both are present —
        it's the most-recent local state. Kept as a
        :func:`staticmethod` on the controller so the private
        stitching stays close to :meth:`status`."""
        last = progress.last_sync_result
        sync_progress_pct: int | None = None
        sync_step = ""
        sync_reason = ""
        sync_error = ""
        if last is not None and last.commit_sha == local_sha:
            sync_reason = last.reason or ""
            sync_error = last.error or ""
            if last.in_progress:
                sync_progress_pct = last.progress_percentage
                sync_step = last.current_step or ""
        if progress.applying and progress.apply_total > 0:
            sync_progress_pct = int(progress.apply_done * 100 / progress.apply_total)
            sync_step = progress.apply_step or "indexing"
        return sync_progress_pct, sync_step, sync_reason, sync_error

    def _resolve_install_state(self, sync: CodeIndexSyncManager) -> _InstallState:
        """Derive install-state fields from the resolver's cache.

        When the resolver hasn't yet resolved this session, fire a
        best-effort background resolve so the next poll has the
        answer, and return ``"unknown"`` for now.
        """
        resolved = sync.resolver.cached if sync.resolver else None
        if resolved is None and sync.resolver is not None:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(sync.resolver.resolve())
        if resolved is None:
            return _InstallState("unknown", "", "")
        if resolved.needs_install:
            return _InstallState("needs_install", "", resolved.install_url or "")
        return _InstallState("installed", resolved.repository_id or "", "")

    @staticmethod
    def _derive_last_sync(
        sync: CodeIndexSyncManager, progress: SyncProgressSnapshot
    ) -> tuple[str, LastSyncStats]:
        """Compose ``(last_sync_at, last_sync_stats)`` from the
        activity ring buffer (preferred) or the cached
        ``last_sync_result`` (fallback).

        Uses the public :meth:`recent_activity` accessor so this
        stays free of dataclass-import coupling — the entry's
        ``ts`` / ``items_*`` fields are all we need."""
        recent = sync.recent_activity()
        if recent:
            top = recent[0]
            return top.ts, LastSyncStats(
                items_upserted=top.items_upserted,
                items_deleted=top.items_deleted,
            )
        last = progress.last_sync_result
        if last is not None and last.stats:
            return "", LastSyncStats(
                items_upserted=last.stats.items_upserted,
                items_deleted=last.stats.items_deleted,
            )
        return "", LastSyncStats()


class _InstallState:
    """Internal 3-tuple for :meth:`CodeIndexController._resolve_install_state`.

    Kept as a class (rather than a bare tuple or dict) so the
    call sites in :meth:`CodeIndexController.status` read
    ``install.state`` / ``install.repository_id`` /
    ``install.install_url`` instead of positional indexing.
    """

    __slots__ = ("state", "repository_id", "install_url")

    def __init__(self, state: str, repository_id: str, install_url: str) -> None:
        self.state = state
        self.repository_id = repository_id
        self.install_url = install_url
