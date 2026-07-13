"""CodeIndex panel + slash-command RPCs.

Extracted from :mod:`ember_code.backend.server`. Seven free
functions taking ``BackendServer`` as their first arg — the
class holds one-line delegates:

* :func:`codeindex_status` — cheap poll-friendly snapshot for
  the panel header (head-indexed flag, sync progress, install
  state, per-branch dir sizes).
* :func:`codeindex_sync` / :func:`codeindex_resync` — pull /
  wipe-and-pull. Both refresh codeindex availability so the
  agent's system prompt flips between the ``main_agent.md`` /
  ``main_agent.codeindex.md`` variants immediately.
* :func:`codeindex_clean` — drop commits past retention rules.
* :func:`codeindex_head_breakdown` — repo-at-HEAD language
  histogram + recent commit list; slightly heavier so the
  panel calls it on open / after sync, not on every poll.
* :func:`codeindex_activity` — recent sync events for the
  activity log.
* :func:`codeindex_install` — portal URL derived from the
  session's ``api_url`` for the "install GitHub App" button.

Rule 2 clean — no inline imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel

from ember_code.core.code_index.paths import commit_chroma_path

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


class CodeIndexSyncResult(BaseModel):
    """Wire shape for :func:`codeindex_sync` (and, with
    ``forgot=True``, :func:`codeindex_resync`)."""

    skipped: bool
    reason: str
    commit_sha: str
    error: str
    link_start_url: str
    items_upserted: int
    items_deleted: int
    references_upserted: int
    forgot: bool = False


class CodeIndexCleanResult(BaseModel):
    """Wire shape for :func:`codeindex_clean`."""

    dropped: list[str]


class CommitBreakdown(BaseModel):
    """One entry in :attr:`CodeIndexHeadBreakdown.recent_commits`."""

    sha: str
    full_sha: str
    subject: str
    when: str
    indexed: bool


class LangCount(BaseModel):
    """One entry in :attr:`CodeIndexHeadBreakdown.languages`."""

    ext: str
    count: int


class CodeIndexHeadBreakdown(BaseModel):
    """Wire shape for :func:`codeindex_head_breakdown`. Error paths
    (git missing / non-zero exit) populate ``error`` and leave the
    other fields at their zero defaults."""

    file_count: int
    languages: list[LangCount]
    recent_commits: list[CommitBreakdown]
    files_indexed: int
    languages_indexed: dict[str, int]
    error: str = ""


class CodeIndexInstallResult(BaseModel):
    """Wire shape for :func:`codeindex_install`."""

    install_url: str


class LastSyncStats(BaseModel):
    """Aggregate ``items_upserted`` / ``items_deleted`` — nested
    into :class:`CodeIndexStatus`. All-zero when the most recent
    sync produced no changes (or nothing has synced yet)."""

    items_upserted: int = 0
    items_deleted: int = 0


class BranchIndexEntry(BaseModel):
    """One indexed-commit entry surfaced in the CodeIndex panel's
    "branches indexed" section. Sorted newest-``last_used_at``-first."""

    sha: str
    is_head: bool
    size_bytes: int
    last_used_at: str
    branch_refs: list[str]


class CodeIndexStatus(BaseModel):
    """Poll-friendly snapshot for the panel header — every field is
    cheap to compute (or already cached) so the panel can hit this
    every 2s without stalling the RPC bus.

    ``sync_progress_pct`` is ``None`` (not zero) when no sync is
    in-progress; a zero would render a stalled "0%" bar on the
    panel."""

    local_sha: str
    remote_url: str
    last_synced_sha: str
    index_head: str
    head_indexed: bool
    sync_in_progress: bool
    sync_progress_pct: int | None
    sync_step: str
    sync_reason: str
    sync_error: str
    apply_done: int
    apply_total: int
    apply_step: str
    install_state: str
    repository_id: str
    install_url: str
    commits_indexed: int
    index_size_bytes: int
    branches_indexed: list[BranchIndexEntry]
    last_sync_at: str
    last_sync_stats: LastSyncStats


def _dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


async def codeindex_status(backend: "BackendServer") -> CodeIndexStatus:
    """Status snapshot for the CodeIndex panel header.

    Focuses on the *current commit*: whether HEAD is indexed
    locally, whether the server is still indexing it (with the
    latest preflight progress %), and the install state.

    Designed to be cheap and read-only so the panel can poll it
    every couple of seconds without firing extra ``sync_now``
    round-trips — ``sync_progress_pct`` / ``sync_step`` come
    from ``_last_sync_result``, which the watcher (or a manual
    sync) populates on its own cadence.

    Async because ``current_sha`` shells out to ``git`` — running
    it inline on the event loop blocks every other session's RPC
    for the duration of the subprocess (worst case 5 s timeout).
    """
    sync = backend._session.code_index_sync
    index = backend._session.code_index
    state = index.manifest.load()
    local_sha = (await asyncio.to_thread(sync.current_sha)) or ""
    head_indexed = bool(local_sha) and local_sha in state.commits

    last = sync._last_sync_result
    # ``sync_in_progress`` is True for either a server-side
    # IN_PROGRESS preflight *or* a local apply-delta currently
    # running. Both stretch the panel's "syncing…" state; the
    # apply-side progress is what saved ``/codeindex resync``
    # from looking frozen during the embedding-heavy snapshot
    # apply.
    sync_in_progress = (
        bool(sync._in_progress_sha and sync._in_progress_sha == local_sha) or sync._applying
    )
    # Only surface pct/step when the cached result is *for the
    # current HEAD* and still in-progress. A stale in-progress
    # result from a previous sha would otherwise paint the wrong
    # commit's progress.
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
    # Local apply takes precedence: it's actually running *now*,
    # while ``last.in_progress`` is the most recent preflight
    # report which may be stale.
    if sync._applying and sync._apply_total > 0:
        sync_progress_pct = int(sync._apply_done * 100 / sync._apply_total)
        sync_step = sync._apply_step or "indexing"

    resolved = sync.resolver.cached if sync.resolver else None
    # Lazy resolver kick — when HEAD was already indexed locally
    # ``sync_now`` short-circuits before calling ``resolve()``, so
    # ``cached`` would otherwise stay None and the panel would
    # render "GitHub App: unknown" forever. Fire-and-forget so
    # this call stays cheap; the next poll picks up the result.
    if resolved is None and sync.resolver is not None:
        # No loop = rare; panel will retry shortly.
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(sync.resolver.resolve())
    if resolved is None:
        install_state = "unknown"
        repository_id = ""
        install_url = ""
    elif resolved.needs_install:
        install_state = "needs_install"
        repository_id = ""
        install_url = resolved.install_url or ""
    else:
        install_state = "installed"
        repository_id = resolved.repository_id or ""
        install_url = ""
    # Index stats — cheap walk of the per-commit chroma dirs to
    # compute total size on disk. Doing it inline avoids a
    # background scheduler for what is, in practice, a quick walk
    # (each commit dir is a small chroma snapshot).
    index_size_bytes = 0
    branches_indexed: list[BranchIndexEntry] = []
    for sha, info in state.commits.items():
        chroma_dir = commit_chroma_path(index.project, sha, data_dir=index.data_dir)
        size = _dir_size(chroma_dir)
        index_size_bytes += size
        branches_indexed.append(
            BranchIndexEntry(
                sha=sha,
                is_head=sha == state.head,
                size_bytes=size,
                last_used_at=info.last_used_at,
                branch_refs=list(info.branch_refs),
            )
        )
    # Newest-first so the panel can show the most recently used
    # commit at the top of the "branches indexed" list.
    branches_indexed.sort(key=lambda c: c.last_used_at, reverse=True)

    last_sync_at = ""
    last_sync_stats = LastSyncStats()
    recent = sync.recent_activity()
    if recent:
        top = recent[0]
        last_sync_at = top.ts
        last_sync_stats = LastSyncStats(
            items_upserted=top.items_upserted,
            items_deleted=top.items_deleted,
        )
    elif last and last.stats:
        last_sync_stats = LastSyncStats(
            items_upserted=last.stats.items_upserted,
            items_deleted=last.stats.items_deleted,
        )

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
        apply_done=sync._apply_done if sync._applying else 0,
        apply_total=sync._apply_total if sync._applying else 0,
        apply_step=sync._apply_step if sync._applying else "",
        install_state=install_state,
        repository_id=repository_id,
        install_url=install_url,
        commits_indexed=len(state.commits),
        index_size_bytes=index_size_bytes,
        branches_indexed=branches_indexed,
        last_sync_at=last_sync_at,
        last_sync_stats=last_sync_stats,
    )


async def codeindex_sync(backend: "BackendServer", sha: str | None) -> CodeIndexSyncResult:
    """Pull and apply a changeset. ``sha=None`` defaults to HEAD.

    ``link_start_url`` surfaces the install URL when the server
    returned ``LINK_REQUIRED`` — the panel opens it in a browser
    and prompts the user to retry.
    """
    result = await backend._session.code_index_sync.sync_now(sha=sha)
    # If this sync flipped the codeindex from absent → present
    # (or vice versa), rebuild the agent pool + main team so the
    # system prompt matches reality (``main_agent.codeindex.md``
    # vs ``main_agent.md``). Without this, an agent built at
    # session start with an empty chroma keeps saying
    # "CodeIndex isn't active" even after a successful sync.
    try:
        backend._session.refresh_codeindex_availability()
    except Exception as exc:
        logger.debug("refresh_codeindex_availability after sync failed (%s)", exc)
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


async def codeindex_resync(backend: "BackendServer", sha: str | None) -> CodeIndexSyncResult:
    """Wipe the local chroma for ``sha`` (defaults to HEAD) and pull
    a fresh snapshot. Mirrors ``/codeindex resync`` for panel use —
    the underlying recovery path is identical: ``forget_commit`` +
    ``sync_now(force_snapshot=True)``.
    """
    target_sha = sha or (await asyncio.to_thread(backend._session.code_index_sync.current_sha))
    forgot = False
    if target_sha:
        forgot = await backend._session.code_index.forget_commit(target_sha)
    result = await backend._session.code_index_sync.sync_now(sha=target_sha, force_snapshot=True)
    # Same rebuild as ``codeindex_sync``: ``forget_commit`` cleared
    # the chroma and the snapshot just refilled it. The avail flag
    # was likely False during forget, True after the snapshot — so
    # the agent definitely needs the codeindex prompt variant now.
    try:
        backend._session.refresh_codeindex_availability()
    except Exception as exc:
        logger.debug("refresh_codeindex_availability after resync failed (%s)", exc)
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


async def codeindex_clean(backend: "BackendServer") -> CodeIndexCleanResult:
    """Drop commits past the retention rules (selective: keeps
    HEAD and every branch tip). Returns the SHAs that were
    dropped so the panel header can refresh."""
    dropped = await backend._session.code_index.clean()
    return CodeIndexCleanResult(dropped=list(dropped))


async def codeindex_head_breakdown(backend: "BackendServer") -> CodeIndexHeadBreakdown:
    """Repo-at-HEAD signal for the panel: tracked file count
    broken down by language/extension, the last few commits
    with their indexed-or-not flag, AND per-extension indexed
    counts (for the donut's coverage overlay). Slightly heavier
    than ``codeindex_status`` (one chroma scan + git calls), so
    the panel fetches it on open and after each sync — not on
    every 2-second poll.
    """
    project = backend._session.project_dir
    # ``git`` calls run in a thread so a slow git invocation (or its
    # 5s timeout) doesn't block the event loop — under multi-session
    # load this RPC used to stall every other session's dispatch.
    try:
        files = await asyncio.to_thread(
            subprocess.run,
            ["git", "ls-files"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return CodeIndexHeadBreakdown(
            file_count=0,
            languages=[],
            recent_commits=[],
            files_indexed=0,
            languages_indexed={},
            error="git not available",
        )
    if files.returncode != 0:
        return CodeIndexHeadBreakdown(
            file_count=0,
            languages=[],
            recent_commits=[],
            files_indexed=0,
            languages_indexed={},
            error=files.stderr.strip() or "git ls-files failed",
        )

    tracked = [p for p in files.stdout.splitlines() if p]
    ext_counts: Counter[str] = Counter()
    for path in tracked:
        i = path.rfind(".")
        ext = path[i + 1 :].lower() if i > 0 and i < len(path) - 1 else ""
        ext_counts[(ext or "(other)")] += 1
    top_langs = [LangCount(ext=ext, count=n) for ext, n in ext_counts.most_common(10)]

    # Last 5 commits + indexed flag.
    state = backend._session.code_index.manifest.load()
    indexed_shas = set(state.commits.keys())
    try:
        log = await asyncio.to_thread(
            subprocess.run,
            ["git", "log", "-5", "--pretty=format:%H%x09%h%x09%s%x09%cr"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log = None
    recent_commits: list[CommitBreakdown] = []
    if log and log.returncode == 0:
        for line in log.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            full, short, subj, when = parts[:4]
            recent_commits.append(
                CommitBreakdown(
                    sha=short,
                    full_sha=full,
                    subject=subj,
                    when=when,
                    indexed=full in indexed_shas,
                )
            )

    # Per-language indexed counts (HEAD only).
    head_sha = state.head or ""
    files_indexed = 0
    languages_indexed: dict[str, int] = {}
    if head_sha:
        try:
            head = await backend._session.code_index.head_stats(head_sha)
            files_indexed = head.files_indexed
            languages_indexed = dict(head.languages_indexed)
        except Exception as exc:
            logger.debug("head_stats failed: %s", exc)

    return CodeIndexHeadBreakdown(
        file_count=len(tracked),
        languages=top_langs,
        recent_commits=recent_commits,
        files_indexed=files_indexed,
        languages_indexed=languages_indexed,
    )


def codeindex_activity(backend: "BackendServer") -> list[dict]:
    """Recent sync events for the panel's activity log."""
    return [asdict(e) for e in backend._session.code_index_sync.recent_activity()]


def codeindex_install(backend: "BackendServer") -> CodeIndexInstallResult:
    """Return the URL of the Ember portal's repositories page.

    The portal lists the user's connected repos and has an
    ``Add repository`` button that drives the actual GitHub-App
    install flow. The panel opens this URL in a browser; we
    don't try to short-circuit by computing a per-repo install
    URL via ``resolver.resolve`` because:

    * It requires a live API round-trip (and a valid cloud
      token), which fails with a confusing "Could not reach
      Ember Cloud" error when the user simply isn't logged in.
    * The portal page is the same target regardless of repo
      state — if already installed, the user sees their repo
      in the list; if not, they click ``Add repository``.

    Derives the portal host from ``api_url`` by stripping the
    ``api`` token from the leftmost host segment:

    * ``api.ignite-ember.sh`` → ``ignite-ember.sh``
    * ``dev-api.ignite-ember.sh`` → ``dev.ignite-ember.sh``
    * ``staging-api.example.com`` → ``staging.example.com``

    Hosts without an ``api`` token in the leftmost segment are
    passed through unchanged.
    """
    parsed = urlparse(backend._session.settings.api_url)
    host = parsed.netloc
    first, sep, rest = host.partition(".")
    if first == "api":
        # ``api.example.com`` → ``example.com``
        new_host = rest or host
    elif first.endswith("-api"):
        # ``dev-api.example.com`` → ``dev.example.com``
        new_host = f"{first[: -len('-api')]}{sep}{rest}"
    elif first.startswith("api-"):
        # ``api-dev.example.com`` → ``dev.example.com`` (symmetric).
        new_host = f"{first[len('api-') :]}{sep}{rest}"
    else:
        new_host = host
    portal_url = urlunparse((parsed.scheme or "https", new_host, "", "", "", ""))
    return CodeIndexInstallResult(install_url=f"{portal_url.rstrip('/')}/repositories")
