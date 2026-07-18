"""Typed wire schemas for the ``BackendServer.codeindex_*`` panel RPCs.

Extracted out of :mod:`ember_code.backend.server_codeindex` — the old
module inlined ten Pydantic models alongside a 275-LoC controller and
a stack of dead free-function shims. Every wire shape the CodeIndex
panel poll consumes now lives here, matching the sibling
:mod:`schemas_history` / :mod:`schemas_hitl` / :mod:`schemas_run`
convention already in this directory.

.. note::

   The sibling :mod:`schemas_codeindex` module is intentionally
   separate: it holds the ``/codeindex`` slash-command *chat views*
   (``CommandResult`` renderers used by ``cmd_codeindex.py``) while
   this ``_rpc`` module holds the wire shapes for the panel-poll
   RPCs. The two are disjoint concerns — keep it that way.

Consumers:

* :class:`CodeIndexStatus` — poll-friendly snapshot for the panel
  header. Composed from :class:`SyncProgressSnapshot` (returned by
  :meth:`CodeIndexSyncManager.progress_snapshot`) so the controller
  never reaches into the sync manager's private ``_``-prefixed
  fields.
* :class:`CodeIndexSyncResult` / :class:`CodeIndexCleanResult` —
  return shapes for the ``sync`` / ``resync`` / ``clean`` RPCs.
* :class:`CodeIndexHeadBreakdown` / :class:`CommitBreakdown` /
  :class:`LangCount` — the panel's "at HEAD" language histogram +
  recent-commit list.
* :class:`CodeIndexInstallResult` — carries the portal URL for the
  install button. Its :meth:`from_api_url` classmethod owns the
  URL-rewrite recipe (api.foo → foo, foo-api.bar → foo.bar) so the
  controller stays a one-liner.
* :class:`CodeIndexActivityEntry` — wire name for the
  :class:`ember_code.core.code_index.sync.ActivityEntry` Pydantic
  model. Both names refer to the same class (see the alias below);
  the wire name is kept so downstream TS-generated types stay
  stable, and the old ``.from_dataclass`` adapter disappears
  because the internal shape *is* the wire shape now.
* :class:`SyncProgressSnapshot` — Pydantic capture of the sync-
  manager fields the panel needs. Building this in
  :meth:`CodeIndexSyncManager.progress_snapshot` seals the
  private-attr reach-ins previously done inline in the controller.
* :class:`RefreshAvailabilityResult` — Result-shaped return of
  :meth:`Session.refresh_codeindex_availability`, replacing the
  two identical bare ``except Exception`` blocks around it.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel

from ember_code.core.code_index.sync.schemas import (
    ActivityEntry,
)


class CodeIndexSyncResult(BaseModel):
    """Wire shape for :meth:`CodeIndexController.sync` (and, with
    ``forgot=True``, :meth:`CodeIndexController.resync`)."""

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
    """Wire shape for :meth:`CodeIndexController.clean`."""

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
    """Wire shape for :meth:`CodeIndexController.head_breakdown`."""

    file_count: int
    languages: list[LangCount]
    recent_commits: list[CommitBreakdown]
    files_indexed: int
    languages_indexed: dict[str, int]
    error: str = ""


class CodeIndexInstallResult(BaseModel):
    """Wire shape for :meth:`CodeIndexController.install`.

    Owns the api-URL → portal-URL rewrite recipe as a classmethod
    so the controller becomes a one-liner and the transform is
    testable in isolation without spinning up a Session.
    """

    install_url: str

    @classmethod
    def from_api_url(cls, api_url: str) -> CodeIndexInstallResult:
        """Rewrite the api hostname into the portal hostname and
        return the ``/repositories`` page URL.

        Rules (checked in order against the host's leading label):

        * ``api.foo.tld`` → ``foo.tld``
        * ``xxx-api.foo.tld`` → ``xxx.foo.tld``
        * ``api-xxx.foo.tld`` → ``xxx.foo.tld``
        * otherwise the host is used as-is.
        """
        parsed = urlparse(api_url)
        host = parsed.netloc
        first, sep, rest = host.partition(".")
        if first == "api":
            new_host = rest or host
        elif first.endswith("-api"):
            new_host = f"{first[: -len('-api')]}{sep}{rest}"
        elif first.startswith("api-"):
            new_host = f"{first[len('api-') :]}{sep}{rest}"
        else:
            new_host = host
        portal_url = urlunparse((parsed.scheme or "https", new_host, "", "", "", ""))
        return cls(install_url=f"{portal_url.rstrip('/')}/repositories")


class LastSyncStats(BaseModel):
    """Aggregate ``items_upserted`` / ``items_deleted``."""

    items_upserted: int = 0
    items_deleted: int = 0


class BranchIndexEntry(BaseModel):
    """One indexed-commit entry surfaced in the CodeIndex panel's
    "branches indexed" section."""

    sha: str
    is_head: bool
    size_bytes: int
    last_used_at: str
    branch_refs: list[str]


class CodeIndexStatus(BaseModel):
    """Poll-friendly snapshot for the panel header."""

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


# The wire names for two internal types — both aliases refer to
# the same Pydantic classes defined in
# :mod:`ember_code.core.code_index.sync.schemas`. Kept as aliases
# (not separate models) so:
#   1. ``list[CodeIndexActivityEntry]`` on the wire is byte-
#      identical to ``list[ActivityEntry]`` internally — no
#      ``.from_dataclass`` adapter step is needed at the seam.
#   2. :class:`SyncProgressSnapshot` can live in the leaf schemas
#      module so the sync manager constructs it without a
#      circular import against this file.
CodeIndexActivityEntry = ActivityEntry


class RefreshAvailabilityResult(BaseModel):
    """Result-shaped return of
    :meth:`Session.refresh_codeindex_availability`.

    ``ok`` is ``True`` when the call completed (regardless of
    whether the availability flag actually flipped —
    :attr:`changed` carries that). ``error`` is a human-readable
    reason string when ``ok`` is ``False``. Callers that used to
    wrap the call in ``try / except Exception`` now branch on
    :attr:`ok` and can log :attr:`error` at debug level.
    """

    ok: bool = True
    changed: bool = False
    error: str = ""
