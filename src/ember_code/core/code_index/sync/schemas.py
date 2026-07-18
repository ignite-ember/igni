"""Pydantic schemas for the code-index sync subpackage.

Two typed models used across the sync coordinator and the wire layer:

* :class:`SyncResult` — return shape of
  :meth:`CodeIndexSyncManager.sync_now`. Carries the outcome of a
  single sync attempt: skipped reason, applied stats, preflight
  status, and (when the server is still indexing) progress hints.
* :class:`ActivityEntry` — one row in the panel's recent-activity
  log. Same wire shape as
  :class:`ember_code.backend.schemas_codeindex_rpc.CodeIndexActivityEntry`
  — kept as one class so the RPC layer re-exports the Pydantic
  type directly without a dataclass↔BaseModel adapter.

Factory classmethods (:meth:`SyncResult.from_preflight`,
:meth:`SyncResult.already_indexed`, …, :meth:`ActivityEntry.from_result`)
own the previously-inline construction of these shapes so the
coordinator body stays a state-machine sketch rather than a
:class:`SyncResult` builder.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ember_code.core.code_index.delta import DeltaStats
from ember_code.core.code_index.fetcher import PreflightResult, PreflightStatus


class SyncResult(BaseModel):
    """Result of one :meth:`CodeIndexSyncManager.sync_now` call.

    ``skipped`` + ``reason`` describe soft no-ops (no git repo, no
    auth, already indexed locally). ``error`` describes a hard
    failure (preflight raised, apply crashed). ``stats`` is only
    populated on a successful apply. ``preflight_status`` is set
    iff we reached the server — the panel branches on it via the
    :attr:`in_progress` / :attr:`needs_link` properties below.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

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

    # ── Factory classmethods (name the inline construction sites) ──

    @classmethod
    def no_code_index(cls) -> SyncResult:
        """Sync was invoked before the CodeIndex was initialized."""
        return cls(skipped=True, reason="code index not initialized")

    @classmethod
    def no_resolver(cls) -> SyncResult:
        """No repository resolver available (bad settings)."""
        return cls(skipped=True, reason="resolver not available")

    @classmethod
    def not_git_repo(cls) -> SyncResult:
        """``git rev-parse HEAD`` returned nothing — not a git repo."""
        return cls(skipped=True, reason="not a git repository")

    @classmethod
    def already_indexed(cls, commit_sha: str) -> SyncResult:
        """Local chroma already has this commit — short-circuit."""
        return cls(
            commit_sha=commit_sha,
            skipped=True,
            reason="already indexed locally",
        )

    @classmethod
    def resolver_unavailable(cls) -> SyncResult:
        """Resolver ran but returned ``None`` — offline / no auth / no access."""
        return cls(
            skipped=True,
            reason="codeindex unavailable (offline, no access, or no auth)",
        )

    @classmethod
    def needs_install(cls, commit_sha: str, install_url: str | None) -> SyncResult:
        """User must install the GitHub App before this repo can be indexed."""
        return cls(
            skipped=True,
            reason="install the GitHub App to enable code-index for this repo",
            commit_sha=commit_sha,
            link_start_url=install_url,
        )

    @classmethod
    def no_repository_id(cls) -> SyncResult:
        """Resolver returned no ``repository_id`` — server-side setup incomplete."""
        return cls(
            skipped=True,
            reason="codeindex unavailable (server returned no repository_id)",
        )

    @classmethod
    def not_authenticated(cls) -> SyncResult:
        """No access token in credentials store."""
        return cls(skipped=True, reason="not authenticated with Ember Cloud")

    @classmethod
    def preflight_failed(cls, commit_sha: str, message: str) -> SyncResult:
        """Preflight round-trip raised — signed URL layer error."""
        return cls(commit_sha=commit_sha, error=message)

    @classmethod
    def apply_error(cls, commit_sha: str, message: str) -> SyncResult:
        """Apply crashed after a successful preflight."""
        return cls(
            commit_sha=commit_sha,
            error=message,
            preflight_status=PreflightStatus.OK,
        )

    @classmethod
    def fetch_error(cls, commit_sha: str, message: str) -> SyncResult:
        """Changeset fetch skipped mid-flight — preflight succeeded but
        the signed-URL layer refused to serve the delta/snapshot."""
        return cls(
            commit_sha=commit_sha,
            error=message,
            preflight_status=PreflightStatus.OK,
        )

    @classmethod
    def success(cls, commit_sha: str, stats: DeltaStats) -> SyncResult:
        """Apply completed cleanly."""
        return cls(
            commit_sha=commit_sha,
            stats=stats,
            preflight_status=PreflightStatus.OK,
        )

    @classmethod
    def from_preflight(cls, pf: PreflightResult, target_sha: str) -> SyncResult | None:
        """Translate a non-OK preflight into a SyncResult.

        Returns ``None`` when preflight is OK — the caller then
        continues into the apply state machine. Replaces the old
        ``_sync_result_for_non_ok_preflight`` static on the manager;
        the per-status branch lives here, once.
        """
        if pf.status == PreflightStatus.OK:
            return None
        common = {
            "commit_sha": target_sha,
            "preflight_status": pf.status,
            "progress_percentage": pf.progress_percentage,
            "current_step": pf.current_step,
            "link_start_url": pf.link_start_url,
        }
        if pf.status == PreflightStatus.IN_PROGRESS:
            reason = (
                f"indexing in progress ({pf.progress_percentage}%): {pf.current_step}"
                if pf.progress_percentage is not None
                else "indexing in progress"
            )
            return cls(skipped=True, reason=reason, **common)
        if pf.status == PreflightStatus.FAILED:
            return cls(
                error=pf.error_message or "indexing failed for this commit",
                **common,
            )
        if pf.status == PreflightStatus.LINK_REQUIRED:
            return cls(
                skipped=True,
                reason="link a GitHub account to enable code-index for this repo",
                **common,
            )
        if pf.status == PreflightStatus.NO_MATCHING_ACCOUNT:
            return cls(
                skipped=True,
                reason="none of your linked GitHub accounts have access to this repo",
                **common,
            )
        if pf.status == PreflightStatus.REPO_NOT_FOUND:
            return cls(
                skipped=True,
                reason="repository is not indexed by Ember",
                **common,
            )
        if pf.status == PreflightStatus.CHANGESET_NOT_FOUND:
            return cls(
                skipped=True,
                reason="commit is not yet queued for indexing",
                **common,
            )
        # Unknown non-OK status — surface it as a skipped-with-reason
        # rather than treating it as OK.
        return cls(skipped=True, reason=f"preflight status: {pf.status}", **common)


class SyncProgressSnapshot(BaseModel):
    """Poll-friendly view of live sync-manager progress.

    Built by :meth:`CodeIndexSyncManager.progress_snapshot` from
    what used to be several ``_``-prefixed manager fields the RPC
    layer read inline. Publishing them as a typed model seals the
    private-attr reach-in and makes the panel's read boundary a
    real conversion.

    Lives in :mod:`sync.schemas` (a leaf) rather than the wire
    module so the manager can construct it without an inline
    import — the wire module re-exports this class under the same
    name for backwards compatibility.
    """

    in_progress_sha: str | None = None
    applying: bool = False
    apply_done: int = 0
    apply_total: int = 0
    apply_step: str = ""
    last_sync_result: SyncResult | None = None


class ActivityEntry(BaseModel):
    """One row in the panel's recent-activity log.

    Wire-identical to (and re-exported as)
    :class:`ember_code.backend.schemas_codeindex_rpc.CodeIndexActivityEntry`
    — there is only one class, defined here, aliased there. Rule 1:
    the internal shape and the wire shape are the same Pydantic type.
    """

    ts: str = ""  # ISO-8601 UTC
    sha: str = ""
    skipped: bool = False
    succeeded: bool = False
    in_progress: bool = False
    reason: str = ""
    error: str = ""
    duration_ms: int = 0
    items_upserted: int = 0
    items_deleted: int = 0

    @classmethod
    def from_result(
        cls, result: SyncResult, *, duration_ms: int, now_utc_iso: str
    ) -> ActivityEntry:
        """Build an activity entry from a completed :class:`SyncResult`.

        Owns the inline construction site that used to live at the
        tail of :meth:`CodeIndexSyncManager.sync_now`.
        """
        stats = result.stats
        return cls(
            ts=now_utc_iso,
            sha=result.commit_sha or "",
            skipped=bool(result.skipped),
            succeeded=bool(result.succeeded),
            in_progress=bool(result.in_progress),
            reason=result.reason or "",
            error=result.error or "",
            duration_ms=duration_ms,
            items_upserted=stats.items_upserted if stats else 0,
            items_deleted=stats.items_deleted if stats else 0,
        )


__all__ = ["ActivityEntry", "SyncProgressSnapshot", "SyncResult"]
