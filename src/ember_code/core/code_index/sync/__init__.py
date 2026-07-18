"""Sync-coordinator subpackage for the local code-index.

The public surface is :class:`CodeIndexSyncManager` and
:class:`SyncResult` — everything else is a domain class the
coordinator composes internally. Re-exported here so external
callers keep writing::

    from ember_code.core.code_index.sync import (
        CodeIndexSyncManager,
        SyncResult,
    )

or, via the outer :mod:`ember_code.core.code_index` package,
``from ember_code.core.code_index import CodeIndexSyncManager,
SyncResult`` (the outer :mod:`__init__` re-points to us).
"""

from ember_code.core.code_index.sync.activity_log import SyncActivityLog
from ember_code.core.code_index.sync.apply_progress import (
    ApplyProgress,
    ApplyProgressSnapshot,
)
from ember_code.core.code_index.sync.git_head import GitHead
from ember_code.core.code_index.sync.head_watcher import HeadWatcher
from ember_code.core.code_index.sync.manager import CodeIndexSyncManager
from ember_code.core.code_index.sync.retry_ledger import InProgressRetryLedger
from ember_code.core.code_index.sync.schemas import (
    ActivityEntry,
    SyncProgressSnapshot,
    SyncResult,
)

__all__ = [
    "ActivityEntry",
    "ApplyProgress",
    "ApplyProgressSnapshot",
    "CodeIndexSyncManager",
    "GitHead",
    "HeadWatcher",
    "InProgressRetryLedger",
    "SyncActivityLog",
    "SyncProgressSnapshot",
    "SyncResult",
]
