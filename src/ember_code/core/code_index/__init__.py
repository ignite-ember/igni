"""Local code index — semantic code intelligence over a project's source.

Per-project, per-commit Chroma indexes copy-on-write from the parent
commit; the manifest tracks lineage and retention. SQLite (per-project
``state.db``) holds the relational data — file references and commit
metadata.
"""

from ember_code.core.code_index.delta import (
    DeltaApplier,
    DeltaError,
    DeltaResult,
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
    Manifest,
    ManifestStore,
)
from ember_code.core.code_index.resolver import (
    DiscoveryStatus,
    RepositoryResolver,
    ResolvedRepository,
)
from ember_code.core.code_index.schema.chroma_row import (
    ChromaChunkRow,
    ChromaRowMetadata,
)
from ember_code.core.code_index.schema.manifest import (
    CommitInfo,
    ManifestState,
    ManifestWire,
)
from ember_code.core.code_index.schema.stats import HeadStats
from ember_code.core.code_index.schema.where_filter import ChromaWhereFilter
from ember_code.core.code_index.sync import (
    ActivityEntry,
    CodeIndexSyncManager,
    SyncResult,
)

__all__ = [
    "ActivityEntry",
    "ChangesetFetchError",
    "ChangesetFetcher",
    "ChromaChunkRow",
    "ChromaRowMetadata",
    "ChromaWhereFilter",
    "CodeIndex",
    "CodeIndexSyncManager",
    "CommitInfo",
    "CommitNotFoundError",
    "DeltaApplier",
    "DeltaError",
    "DeltaResult",
    "DeltaStats",
    "DiscoveryStatus",
    "HeadStats",
    "Manifest",
    "ManifestState",
    "ManifestStore",
    "ManifestWire",
    "PreflightResult",
    "PreflightStatus",
    "RepositoryResolver",
    "ResolvedRepository",
    "SyncResult",
    "apply_delta",
]
