"""Local code index — semantic code intelligence over a project's source.

Per-project, per-commit Chroma indexes copy-on-write from the parent
commit; the manifest tracks lineage and retention. SQLite (per-project
``state.db``) holds the relational data — file references and commit
metadata.
"""

from ember_code.core.code_index.delta import (
    DeltaError,
    DeltaStats,
    apply_delta,
)
from ember_code.core.code_index.fetcher import (
    ChangesetFetcher,
    ChangesetFetchError,
    PreflightResult,
    PreflightStatus,
)
from ember_code.core.code_index.index import CodeIndex, CommitNotFoundError
from ember_code.core.code_index.manifest import (
    CommitInfo,
    Manifest,
    ManifestState,
)
from ember_code.core.code_index.resolver import (
    DiscoveryStatus,
    RepositoryResolver,
    ResolvedRepository,
)
from ember_code.core.code_index.sync_manager import CodeIndexSyncManager, SyncResult

__all__ = [
    "ChangesetFetchError",
    "ChangesetFetcher",
    "CodeIndex",
    "CodeIndexSyncManager",
    "CommitInfo",
    "CommitNotFoundError",
    "DeltaError",
    "DeltaStats",
    "DiscoveryStatus",
    "Manifest",
    "ManifestState",
    "PreflightResult",
    "PreflightStatus",
    "RepositoryResolver",
    "ResolvedRepository",
    "SyncResult",
    "apply_delta",
]
