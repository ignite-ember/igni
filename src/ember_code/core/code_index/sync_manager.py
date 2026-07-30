"""Backward-compat shim — the real code lives in :mod:`sync`.

This module was refactored into a
:mod:`ember_code.core.code_index.sync` subpackage (five domain
classes + a coordinator). External imports were previously
written as::

    from ember_code.core.code_index.sync_manager import (
        CodeIndexSyncManager, SyncResult,
    )

Those keep working via the re-exports below. New code should
import from :mod:`ember_code.core.code_index.sync` directly.
"""

from __future__ import annotations

# Re-export ``ChangesetFetcher`` as well so old tests that did
# ``monkeypatch.setattr(sm.ChangesetFetcher, "preflight", ...)``
# keep patching the real class the coordinator uses.
from ember_code.core.code_index.fetcher import ChangesetFetcher
from ember_code.core.code_index.sync import (
    ActivityEntry,
    ApplyProgress,
    CodeIndexSyncManager,
    GitHead,
    HeadWatcher,
    InProgressRetryLedger,
    SyncActivityLog,
    SyncResult,
)

__all__ = [
    "ActivityEntry",
    "ApplyProgress",
    "ChangesetFetcher",
    "CodeIndexSyncManager",
    "GitHead",
    "HeadWatcher",
    "InProgressRetryLedger",
    "SyncActivityLog",
    "SyncResult",
]
