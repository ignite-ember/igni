"""High-level orchestration: download per-commit changesets and apply them.

``CodeIndexSyncManager`` is the single entry point used by Session
startup, the ``/codeindex sync`` slash command, the HEAD watcher, and
the ``/clear`` handler. It:

- discovers the repo's ``repository_id`` from ember-server via
  :class:`RepositoryResolver` (no user config required)
- builds a :class:`ChangesetFetcher` lazily once we have a bearer token
- serializes concurrent calls through an asyncio lock
- degrades to a no-op (with a clear ``reason``) when the project isn't
  a git repo, the user isn't authenticated, the repo isn't registered,
  or the user lacks access

Failure modes return :class:`SyncResult` rather than raising — the
caller decides whether to surface a warning or stay silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.code_index.delta import DeltaStats
from ember_code.core.code_index.fetcher import (
    ChangesetFetcher,
    ChangesetFetchError,
    PreflightResult,
    PreflightStatus,
)
from ember_code.core.code_index.resolver import RepositoryResolver

if TYPE_CHECKING:
    from ember_code.core.code_index.index import CodeIndex
    from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_WATCH_INTERVAL_SECONDS = 1.0  # local git rev-parse poll cadence

# When preflight returns IN_PROGRESS, retry every N seconds. Flat (no
# exponential backoff): indexing rarely finishes faster than this and a
# steady cadence is easier to reason about + show in the UI.
IN_PROGRESS_RETRY_SECONDS = 15.0


@dataclass
class SyncResult:
    skipped: bool = False
    reason: str | None = None
    commit_sha: str | None = None
    stats: DeltaStats | None = None
    error: str | None = None
    # Populated when preflight runs (i.e. sync_now reached the server).
    preflight_status: PreflightStatus | None = None
    progress_percentage: int | None = None
    current_step: str | None = None
    link_start_url: str | None = None

    @property
    def succeeded(self) -> bool:
        return not self.skipped and self.error is None and self.stats is not None

    @property
    def in_progress(self) -> bool:
        """True when the server is still indexing — caller should retry."""
        return self.preflight_status == PreflightStatus.IN_PROGRESS

    @property
    def needs_link(self) -> bool:
        """True when the user must complete a GitHub link flow before retrying."""
        return self.preflight_status in (
            PreflightStatus.LINK_REQUIRED,
            PreflightStatus.NO_MATCHING_ACCOUNT,
        )


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
        self._fetcher: ChangesetFetcher | None = None
        self._last_synced_sha: str | None = None
        self._lock = asyncio.Lock()
        self._watcher_task: asyncio.Task | None = None
        self._watch_interval = DEFAULT_WATCH_INTERVAL_SECONDS
        # In-progress retry state, scoped to a single sha. Cleared whenever
        # the server returns any non-in-progress status, or HEAD moves.
        self._in_progress_sha: str | None = None
        self._next_retry_at: float | None = None
        # Cached so the CodeIndex panel's status poll can read live
        # progress (preflight pct + current_step) without re-firing
        # ``sync_now`` itself. Populated on every ``sync_now`` return,
        # including watcher-driven retries — so the panel reflects the
        # state of the most recent background poll, not the user's last
        # manual click.
        self._last_sync_result: SyncResult | None = None
        # Live apply-progress, updated by ``apply_delta``'s callback
        # while a sync is running. ``codeindex_status`` surfaces these
        # so ``/codeindex resync`` and ``/codeindex sync`` can render
        # ``Resyncing N/M · current_item`` instead of looking frozen
        # while embeddings churn for ~2s per item. Reset on each new
        # sync call. ``_applying`` is True only while ``apply_delta``
        # is actually running — it gates ``codeindex_status``'s
        # ``sync_in_progress`` so a long-completed sync doesn't keep
        # showing "100% done" after the FE has dismissed it.
        self._applying: bool = False
        self._apply_done: int = 0
        self._apply_total: int = 0
        self._apply_step: str = ""

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
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None

    # ── Sync ─────────────────────────────────────────────────────────

    async def sync_now(self, *, sha: str | None = None, force_snapshot: bool = False) -> SyncResult:
        """Download and apply the changeset for ``sha`` (defaults to current HEAD).

        ``force_snapshot`` skips the delta-vs-snapshot routing and always
        pulls the full snapshot — used by ``/codeindex resync`` to recover
        from a drifted local index.
        """
        async with self._lock:
            result = await self._sync_locked(sha=sha, force_snapshot=force_snapshot)
            self._last_sync_result = result
            return result

    def _on_apply_progress(self, done: int, total: int, label: str) -> None:
        """Callback fed to ``apply_delta`` to surface real-time progress.

        The TUI polls ``BackendServer.codeindex_status`` while a
        ``/codeindex resync`` is running; reading these three fields
        keeps the busy label moving instead of stuck at
        ``Resyncing (full snapshot)…`` for the ~30-90s an apply
        takes on a fresh checkout.
        """
        self._apply_done = done
        self._apply_total = total
        self._apply_step = label

    def _reset_apply_progress(self) -> None:
        self._applying = False
        self._apply_done = 0
        self._apply_total = 0
        self._apply_step = ""

    async def _sync_locked(self, *, sha: str | None, force_snapshot: bool = False) -> SyncResult:
        # Start each sync with a clean progress slate so a poll
        # arriving right at sync start doesn't show leftovers from
        # a previous run.
        self._reset_apply_progress()
        if self.code_index is None:
            return SyncResult(skipped=True, reason="code index not initialized")
        if self.resolver is None:
            return SyncResult(skipped=True, reason="resolver not available")

        target_sha = sha or self.current_sha()
        if not target_sha:
            return SyncResult(skipped=True, reason="not a git repository")

        resolved = await self.resolver.resolve()
        if resolved is None:
            return SyncResult(
                skipped=True,
                reason="codeindex unavailable (offline, no access, or no auth)",
            )

        if resolved.needs_install:
            return SyncResult(
                skipped=True,
                reason="install the GitHub App to enable code-index for this repo",
                commit_sha=target_sha,
                link_start_url=resolved.install_url,
            )

        if not resolved.repository_id:
            return SyncResult(
                skipped=True,
                reason="codeindex unavailable (server returned no repository_id)",
            )

        token = self.credentials.access_token if self.credentials else None
        if not token:
            return SyncResult(skipped=True, reason="not authenticated with Ember Cloud")

        if self._fetcher is None:
            self._fetcher = ChangesetFetcher(
                server_url=self.server_url,
                bearer_token=token,
                timeout=self.fetch_timeout,
            )
        else:
            # Refresh the bearer token in case it rotated since last sync.
            self._fetcher.bearer_token = token

        # Preflight first — tells us whether to download, poll, or surface a
        # link prompt. Avoids the old "blind 403/404 → opaque error" path.
        try:
            pf = await self._fetcher.preflight(
                repository_id=resolved.repository_id,
                commit_sha=target_sha,
            )
        except ChangesetFetchError as exc:
            logger.info("preflight failed (%s)", exc)
            return SyncResult(commit_sha=target_sha, error=str(exc))

        non_ok = self._sync_result_for_non_ok_preflight(pf, target_sha)
        if non_ok is not None:
            return non_ok

        # Delta-vs-snapshot routing. Goal: keep the local chroma as
        # close to the server's definition as possible. A delta JSONL
        # is only safe when we have a local ancestor to copy-on-write
        # from — otherwise the delta gets applied to an empty chroma
        # and the resulting index reflects only what changed in the
        # last commit, not the full state.
        #
        # We previously trusted ``parent_sha is None`` to mean "this is
        # a git root, the delta is the full state". The server doesn't
        # actually guarantee that — it can return ``None`` whenever it
        # doesn't carry parent lineage on the preflight response, even
        # for non-root commits. Treating that case as a delta produced
        # silently broken indexes (see investigation notes in the
        # repo history). The conservative rule below picks the
        # snapshot endpoint whenever we'd otherwise apply a delta to
        # an empty chroma, and falls back to the delta endpoint only
        # when we have a usable local ancestor.
        target_exists = self.code_index.has_commit(target_sha)
        parent_usable = pf.parent_sha is not None and self.code_index.has_commit(pf.parent_sha)
        use_snapshot = force_snapshot or (not target_exists and not parent_usable)
        self._applying = True
        try:
            if use_snapshot:
                reason = "forced" if force_snapshot else "no local ancestor to copy-on-write from"
                logger.info(
                    "sync %s via snapshot (%s; parent_sha=%s)",
                    target_sha[:8],
                    reason,
                    pf.parent_sha[:8] if pf.parent_sha else "None",
                )
                stats = await self._fetcher.pull_and_apply_snapshot(
                    index=self.code_index,
                    file_refs=self.code_index._file_reference_service(),
                    repository_id=resolved.repository_id,
                    commit_sha=target_sha,
                    on_progress=self._on_apply_progress,
                )
            else:
                stats = await self._fetcher.pull_and_apply(
                    index=self.code_index,
                    file_refs=self.code_index._file_reference_service(),
                    repository_id=resolved.repository_id,
                    commit_sha=target_sha,
                    on_progress=self._on_apply_progress,
                )
        except ChangesetFetchError as exc:
            logger.info("sync skipped (%s)", exc)
            return SyncResult(
                commit_sha=target_sha, error=str(exc), preflight_status=PreflightStatus.OK
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("apply_delta failed for %s", target_sha)
            return SyncResult(
                commit_sha=target_sha,
                error=f"apply failed: {exc}",
                preflight_status=PreflightStatus.OK,
            )
        finally:
            self._applying = False

        self._last_synced_sha = target_sha
        logger.info(
            "synced commit %s (%d items, %d refs)",
            target_sha[:8],
            stats.items_upserted,
            stats.references_upserted,
        )
        return SyncResult(commit_sha=target_sha, stats=stats, preflight_status=PreflightStatus.OK)

    @staticmethod
    def _sync_result_for_non_ok_preflight(
        pf: PreflightResult, target_sha: str
    ) -> SyncResult | None:
        """Translate a non-OK preflight into a SyncResult; ``None`` means OK → continue."""
        if pf.status == PreflightStatus.OK:
            return None
        base = SyncResult(
            commit_sha=target_sha,
            preflight_status=pf.status,
            progress_percentage=pf.progress_percentage,
            current_step=pf.current_step,
            link_start_url=pf.link_start_url,
        )
        if pf.status == PreflightStatus.IN_PROGRESS:
            base.skipped = True
            base.reason = (
                f"indexing in progress ({pf.progress_percentage}%): {pf.current_step}"
                if pf.progress_percentage is not None
                else "indexing in progress"
            )
        elif pf.status == PreflightStatus.FAILED:
            base.error = pf.error_message or "indexing failed for this commit"
        elif pf.status == PreflightStatus.LINK_REQUIRED:
            base.skipped = True
            base.reason = "link a GitHub account to enable code-index for this repo"
        elif pf.status == PreflightStatus.NO_MATCHING_ACCOUNT:
            base.skipped = True
            base.reason = "none of your linked GitHub accounts have access to this repo"
        elif pf.status == PreflightStatus.REPO_NOT_FOUND:
            base.skipped = True
            base.reason = "repository is not indexed by Ember"
        elif pf.status == PreflightStatus.CHANGESET_NOT_FOUND:
            base.skipped = True
            base.reason = "commit is not yet queued for indexing"
        return base

    # ── HEAD watcher ─────────────────────────────────────────────────

    async def start_watcher(
        self, *, interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS
    ) -> None:
        """Begin polling git HEAD; fire ``sync_now`` on every detected change."""
        if self._watcher_task is not None and not self._watcher_task.done():
            return
        self._watch_interval = interval_seconds
        self._watcher_task = asyncio.create_task(self._watch_loop())

    async def stop_watcher(self) -> None:
        if self._watcher_task is None:
            return
        self._watcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._watcher_task
        self._watcher_task = None

    async def _watch_loop(self) -> None:
        """1Hz local poll on git HEAD.

        Two trigger conditions for calling sync_now:
        - HEAD moved (the new sha hasn't been synced yet and isn't already
          being polled in_progress).
        - 15s has elapsed since we last polled an in-progress sha that's
          still the current HEAD.

        ``git rev-parse HEAD`` is microseconds; the only network call is
        ``sync_now`` itself, and that only fires when one of the triggers
        above is true.
        """
        loop = asyncio.get_event_loop()
        while True:
            try:
                await asyncio.sleep(self._watch_interval)
                sha = self.current_sha()
                if not sha:
                    continue

                # HEAD moved away from the in_progress sha → drop the retry state.
                if self._in_progress_sha and sha != self._in_progress_sha:
                    self._clear_in_progress()

                head_changed = sha != self._last_synced_sha and sha != self._in_progress_sha
                retry_due = (
                    self._in_progress_sha == sha
                    and self._next_retry_at is not None
                    and loop.time() >= self._next_retry_at
                )

                if not (head_changed or retry_due):
                    continue

                result = await self.sync_now(sha=sha)
                if result.in_progress:
                    self._in_progress_sha = sha
                    self._next_retry_at = loop.time() + IN_PROGRESS_RETRY_SECONDS
                else:
                    self._clear_in_progress()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover — defensive
                logger.exception("HEAD watcher iteration failed")

    def _clear_in_progress(self) -> None:
        self._in_progress_sha = None
        self._next_retry_at = None
