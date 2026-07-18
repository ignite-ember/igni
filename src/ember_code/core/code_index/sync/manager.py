"""High-level coordinator: pull per-commit changesets and apply them.

:class:`CodeIndexSyncManager` is the single entry point used by
Session startup, the ``/codeindex sync`` slash command, the HEAD
watcher, and the ``/clear`` handler. It composes five domain
classes rather than owning their state inline:

* :class:`GitHead` — HEAD SHA lookup via ``git rev-parse``.
* :class:`SyncActivityLog` — ring buffer of recent sync events.
* :class:`ApplyProgress` — live per-item apply counters.
* :class:`InProgressRetryLedger` — the sha we're polling +
  next-retry timestamp.
* :class:`HeadWatcher` — the 1Hz asyncio poll loop.

The manager itself owns only:

- the preflight/apply state machine (:meth:`_sync_locked`),
- the :class:`ChangesetFetcher` (lazily built once we have a
  bearer token),
- the concurrent-call lock,
- ``_last_synced_sha`` (read by the watcher via a getter so it
  stays authoritative on the manager).

Failure modes return :class:`SyncResult` rather than raising —
the caller decides whether to surface a warning or stay silent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.code_index.delta import DeltaError
from ember_code.core.code_index.fetcher import (
    ChangesetFetcher,
    ChangesetFetchError,
    PreflightResult,
)
from ember_code.core.code_index.resolver import RepositoryResolver
from ember_code.core.code_index.sync.activity_log import SyncActivityLog
from ember_code.core.code_index.sync.apply_progress import ApplyProgress
from ember_code.core.code_index.sync.git_head import GitHead
from ember_code.core.code_index.sync.head_watcher import HeadWatcher
from ember_code.core.code_index.sync.retry_ledger import InProgressRetryLedger
from ember_code.core.code_index.sync.schemas import (
    ActivityEntry,
    SyncProgressSnapshot,
    SyncResult,
)

if TYPE_CHECKING:
    # ``CodeIndexActivityEntry`` is a wire-name alias for
    # :class:`ActivityEntry`. Type-only import (a runtime import
    # would cycle: ``schemas_codeindex_rpc`` sits above us).
    from ember_code.backend.schemas_codeindex_rpc import CodeIndexActivityEntry
    from ember_code.core.code_index.index import CodeIndex
    from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class CodeIndexSyncManager:
    """Pull JSONL changesets via signed URLs and replay them locally."""

    def __init__(
        self,
        *,
        project_dir: Path,
        code_index: CodeIndex | None,
        resolver: RepositoryResolver | None,
        credentials: CloudCredentials | None,
        server_url: str,
        fetch_timeout: float = 60.0,
    ) -> None:
        self.project_dir = project_dir
        self.code_index = code_index
        self.resolver = resolver
        self.credentials = credentials
        self.server_url = server_url.rstrip("/")
        self.fetch_timeout = fetch_timeout

        # Composed collaborators — one instance each.
        self._git = GitHead(project_dir)
        self._activity_log = SyncActivityLog()
        self._apply_progress = ApplyProgress()
        self._retry_ledger = InProgressRetryLedger()
        # Watcher gets closures over ``self`` (not captured bound
        # methods) so subclasses / tests can override
        # :meth:`current_sha` and :meth:`sync_now` by replacing the
        # attribute on the manager instance — the lookup happens
        # at call time, not at construction.
        self._watcher = HeadWatcher(
            get_head=lambda: self.current_sha(),
            run_sync=lambda sha: self.sync_now(sha=sha),
            retry_ledger=self._retry_ledger,
            last_synced_sha_getter=lambda: self._last_synced_sha,
        )

        # State the manager still owns.
        self._fetcher: ChangesetFetcher | None = None
        self._last_synced_sha: str | None = None
        self._lock = asyncio.Lock()
        # Cached so the CodeIndex panel's status poll can read live
        # progress (preflight pct + current_step) without re-firing
        # ``sync_now`` itself. Populated on every ``sync_now`` return,
        # including watcher-driven retries — so the panel reflects the
        # state of the most recent background poll, not the user's last
        # manual click.
        self._last_sync_result: SyncResult | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        project_dir: Path,
        code_index: CodeIndex | None,
        credentials: CloudCredentials | None = None,
    ) -> CodeIndexSyncManager:
        creds = credentials or CloudCredentials(settings.auth.credentials_file)
        resolver = RepositoryResolver(
            project_dir=project_dir,
            server_url=settings.api_url,
            credentials=creds,
        )
        return cls(
            project_dir=project_dir,
            code_index=code_index,
            resolver=resolver,
            credentials=creds,
            server_url=settings.api_url,
            fetch_timeout=settings.code_index.fetch_timeout,
        )

    @property
    def last_synced_sha(self) -> str | None:
        return self._last_synced_sha

    @property
    def fetcher(self) -> ChangesetFetcher | None:
        return self._fetcher

    @property
    def repository_id(self) -> str | None:
        if self.resolver is None:
            return None
        cached = self.resolver.cached
        return cached.repository_id if cached else None

    # ── Git HEAD ─────────────────────────────────────────────────────

    def current_sha(self) -> str | None:
        """Return the project's current ``HEAD`` SHA, or ``None`` if not a git repo."""
        return self._git.current_sha()

    # ── Sync ─────────────────────────────────────────────────────────

    async def sync_now(self, *, sha: str | None = None, force_snapshot: bool = False) -> SyncResult:
        """Download and apply the changeset for ``sha`` (defaults to current HEAD).

        ``force_snapshot`` skips the delta-vs-snapshot routing and always
        pulls the full snapshot — used by ``/codeindex resync`` to recover
        from a drifted local index.
        """
        started = time.monotonic()
        async with self._lock:
            result = await self._sync_locked(sha=sha, force_snapshot=force_snapshot)
            self._last_sync_result = result
            self._record_activity(result, started_at=started)
            return result

    def _record_activity(self, result: SyncResult, *, started_at: float) -> None:
        """Append to the activity ring buffer — but skip the "watcher
        tick, nothing changed" no-op entries so the panel only shows
        genuine sync activity."""
        if result.skipped and result.reason == "already indexed locally":
            return
        entry = ActivityEntry.from_result(
            result,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            now_utc_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._activity_log.record(entry)

    def recent_activity(self) -> list[ActivityEntry]:
        """Most recent sync events, newest first. Used by the panel."""
        return self._activity_log.recent()

    def activity_entries(self) -> list[CodeIndexActivityEntry]:
        """Recent activity, newest-first, as typed wire models.

        Since :class:`ActivityEntry` is aliased to
        :class:`CodeIndexActivityEntry` in the schemas layer, the
        two names refer to the same Pydantic class and this is
        just a re-typed view over :meth:`recent_activity` —
        callers see the wire name without an adapter step.
        """
        return list(self.recent_activity())

    def progress_snapshot(self) -> SyncProgressSnapshot:
        """Poll-friendly view of live sync progress.

        Publishes the previously ``_``-prefixed sync-manager
        fields as a typed :class:`SyncProgressSnapshot` so
        :meth:`CodeIndexController.status` no longer reaches
        past the leading underscore. Kept as a *snapshot* method
        (not @property) to signal "reads live mutable state" at
        every call site.
        """
        apply = self._apply_progress.snapshot()
        return SyncProgressSnapshot(
            in_progress_sha=self._retry_ledger.in_progress_sha,
            applying=apply.applying,
            apply_done=apply.apply_done,
            apply_total=apply.apply_total,
            apply_step=apply.apply_step,
            last_sync_result=self._last_sync_result,
        )

    async def _sync_locked(self, *, sha: str | None, force_snapshot: bool = False) -> SyncResult:
        # Start each sync with a clean progress slate so a poll
        # arriving right at sync start doesn't show leftovers from
        # a previous run.
        self._apply_progress.reset()
        if self.code_index is None:
            return SyncResult.no_code_index()
        if self.resolver is None:
            return SyncResult.no_resolver()

        # ``current_sha`` shells out to ``git`` (sync subprocess);
        # offload so the BE's event loop keeps dispatching other
        # sessions' RPCs while git resolves HEAD.
        target_sha = sha or await asyncio.to_thread(self._git.current_sha)
        if not target_sha:
            return SyncResult.not_git_repo()

        # Short-circuit when the target sha is already fully indexed
        # locally. Avoids the resolver call + ``preflight`` round-trip
        # + a delta pull that would be a no-op anyway. ``has_commit``
        # checks BOTH the chroma dir and the manifest entry, so this
        # path can't be taken against a half-deleted index.
        #
        # ``head_indexed`` in ``codeindex_status`` uses the same check
        # against the manifest, so the panel renders ``[green]indexed[/green]``
        # without the misleading ``not indexed · already indexed locally``
        # combo. ``force_snapshot`` (``/codeindex resync``) intentionally
        # bypasses this to recover from a drifted local index.
        if not force_snapshot and self.code_index.has_commit(target_sha):
            self._last_synced_sha = target_sha
            return SyncResult.already_indexed(target_sha)

        resolved = await self.resolver.resolve()
        if resolved is None:
            return SyncResult.resolver_unavailable()

        if resolved.needs_install:
            return SyncResult.needs_install(target_sha, resolved.install_url)

        if not resolved.repository_id:
            return SyncResult.no_repository_id()

        token = self.credentials.access_token if self.credentials else None
        if not token:
            return SyncResult.not_authenticated()

        fetcher = self._ensure_fetcher(token)

        # Preflight first — tells us whether to download, poll, or surface a
        # link prompt. Avoids the old "blind 403/404 → opaque error" path.
        try:
            pf = await fetcher.preflight(
                repository_id=resolved.repository_id,
                commit_sha=target_sha,
            )
        except ChangesetFetchError as exc:
            logger.info("preflight failed (%s)", exc)
            return SyncResult.preflight_failed(target_sha, str(exc))

        non_ok = SyncResult.from_preflight(pf, target_sha)
        if non_ok is not None:
            return non_ok

        use_snapshot = self._should_use_snapshot(
            pf=pf, target_sha=target_sha, force_snapshot=force_snapshot
        )

        file_refs = self.code_index.file_reference_service()
        with self._apply_progress.active_scope():
            try:
                if use_snapshot:
                    reason = (
                        "forced" if force_snapshot else "no local ancestor to copy-on-write from"
                    )
                    logger.info(
                        "sync %s via snapshot (%s; parent_sha=%s)",
                        target_sha[:8],
                        reason,
                        pf.parent_sha[:8] if pf.parent_sha else "None",
                    )
                    stats = await fetcher.pull_and_apply_snapshot(
                        index=self.code_index,
                        file_refs=file_refs,
                        repository_id=resolved.repository_id,
                        commit_sha=target_sha,
                        on_progress=self._apply_progress.update,
                    )
                else:
                    stats = await fetcher.pull_and_apply(
                        index=self.code_index,
                        file_refs=file_refs,
                        repository_id=resolved.repository_id,
                        commit_sha=target_sha,
                        on_progress=self._apply_progress.update,
                    )
            except ChangesetFetchError as exc:
                logger.info("sync skipped (%s)", exc)
                return SyncResult.fetch_error(target_sha, str(exc))
            except DeltaError as exc:
                logger.exception("delta apply failed for %s", target_sha)
                return SyncResult.apply_error(target_sha, f"apply failed: {exc}")
            except Exception as exc:
                # Broad catch is the Pattern-3 boundary — the applier
                # doesn't yet expose a stable typed exception surface,
                # and a raised exception here would kill the sync-lock
                # holder without unwinding _last_synced_sha.
                logger.exception("apply failed for %s", target_sha)
                return SyncResult.apply_error(target_sha, f"apply failed: {exc}")

        self._last_synced_sha = target_sha
        logger.info(
            "synced commit %s (%d items, %d refs)",
            target_sha[:8],
            stats.items_upserted,
            stats.references_upserted,
        )
        return SyncResult.success(target_sha, stats)

    def _should_use_snapshot(
        self, *, pf: PreflightResult, target_sha: str, force_snapshot: bool
    ) -> bool:
        """Route between delta and snapshot endpoints.

        A delta JSONL is only safe when a local ancestor exists to
        copy-on-write from — otherwise the delta applies against an
        empty chroma and the result reflects only the last-commit
        deltas, not the full state. ``parent_sha is None`` from the
        preflight is not a reliable "this is a git root" signal, so
        we fall back to snapshot whenever we lack a usable ancestor.
        """
        if force_snapshot:
            return True
        target_exists = self.code_index.has_commit(target_sha)
        parent_usable = pf.parent_sha is not None and self.code_index.has_commit(pf.parent_sha)
        return not target_exists and not parent_usable

    def _ensure_fetcher(self, token: str) -> ChangesetFetcher:
        """Return the cached :class:`ChangesetFetcher`, refreshing its
        bearer token in case it rotated since the last sync."""
        if self._fetcher is None:
            self._fetcher = ChangesetFetcher(
                server_url=self.server_url,
                bearer_token=token,
                timeout=self.fetch_timeout,
            )
        else:
            self._fetcher.bearer_token = token
        return self._fetcher

    # ── HEAD watcher ─────────────────────────────────────────────────

    async def start_watcher(
        self,
        *,
        interval_seconds: float = HeadWatcher.DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        """Begin polling git HEAD; fire ``sync_now`` on every detected change."""
        await self._watcher.start(interval_seconds=interval_seconds)

    async def stop_watcher(self) -> None:
        """Stop the HEAD watcher's poll loop."""
        await self._watcher.stop()


__all__ = ["CodeIndexSyncManager"]
