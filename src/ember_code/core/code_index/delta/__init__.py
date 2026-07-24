"""JSONL delta contract + applier for the per-commit code index.

Producers (ember-server) emit a JSONL file describing what changed
between the parent commit and the new one. Each line is a single JSON
object with an ``op`` field. :func:`apply_delta` streams the file and
mutates the local chroma index + SQLite reference table accordingly.

Contract — one object per line:

- ``{"op": "commit", "sha": "...", "parent_sha": "...|null", ...}``
  Always the first line. Carries lineage so the applier can
  ``prepare_commit(sha, parent_sha)`` before any data ops.
- ``{"op": "upsert_item", "id": "...", "type": "file|folder|entity", ...}``
  Insert or replace an item. ``id`` is a stable per-path identifier
  (``UUID5(path)``); the same path keeps the same id across commits,
  so a content change on an existing item replaces it in place
  rather than inserting an orphan alongside. The full quality and
  category schema travels on this op — see :class:`UpsertItemOp` for
  every field.
- ``{"op": "delete_item", "id": "..."}`` — remove an item.
- ``{"op": "upsert_reference", "from_id": "...", "to_id": "...", "relation": "...", "meta": {}}``
  Insert or replace a reference. ``relation`` is the canonical edge
  kind ("calls" / "called_by" / "imports" / ...). References live in
  the per-project SQLite (no commit scope) — they persist until
  explicitly deleted.
- ``{"op": "delete_reference", "from_id": "...", "to_id": "..."}``
- ``{"op": "commit_summary", "sha": "...", "markdown": "..."}``
  Server-rendered project map for the commit, persisted next to the
  chroma dir.

Idempotent: applying the same JSONL twice yields the same state. Safe
to retry on partial failure.

Public API — importing anything below directly is stable; the
sub-package layout underneath is an implementation detail:

- :func:`apply_delta` — module-level shim for backward compat.
- :func:`parse_op` / :func:`iter_ops` — bound-method shims on a
  default :class:`DeltaParser` for backward compat.
- :class:`DeltaApplier` / :class:`DeltaParser` — OOP entry points
  for callers that want the richer :class:`DeltaResult` return type.
- :class:`DeltaStats`, :class:`DeltaResult`, :class:`DeltaError`.
- The six op models plus :data:`DeltaOp` (discriminated-union alias)
  and :class:`ReferenceMeta`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ember_code.core.code_index.delta.applier import DeltaApplier
from ember_code.core.code_index.delta.ops import (
    CommitOp,
    CommitSummaryOp,
    DeleteItemOp,
    DeleteReferenceOp,
    DeltaOp,
    ReferenceMeta,
    UpsertItemOp,
    UpsertReferenceOp,
)
from ember_code.core.code_index.delta.parser import DeltaParser
from ember_code.core.code_index.delta.progress import (
    ProgressCallback,
    ProgressReporter,
    SafeProgressReporter,
)
from ember_code.core.code_index.delta.stats import (
    DeltaError,
    DeltaResult,
    DeltaStats,
)

# A single default parser instance backs the module-level ``parse_op``
# and ``iter_ops`` names. Exposing bound methods (rather than free
# functions that recreate a parser on every call) preserves the exact
# callable signature the tests use with zero import churn and keeps
# the module-level names honest — they *are* the OOP entry points,
# just spelled with a shorter name.
_default_parser = DeltaParser()
parse_op: Callable[[str], DeltaOp | None] = _default_parser.parse_line
iter_ops = _default_parser.iter_ops


async def apply_delta(
    *,
    index,
    file_refs,
    jsonl_path: str | Path,
    on_progress: ProgressCallback | None = None,
) -> DeltaStats:
    """Backward-compatibility shim over :class:`DeltaApplier`.

    Constructs a :class:`DeltaApplier` from the passed collaborators,
    awaits :meth:`DeltaApplier.run`, and returns the accumulated
    :class:`DeltaStats` — matching the historical return contract that
    ``index.py`` / ``fetcher.py`` / ``sync_manager.py`` depend on.
    Callers that want the richer :class:`DeltaResult` (with ``ok`` /
    ``reason``) should construct :class:`DeltaApplier` directly.
    """
    applier = DeltaApplier(
        index=index,
        file_refs=file_refs,
        jsonl_path=jsonl_path,
        on_progress=on_progress,
    )
    result = await applier.run()
    return result.stats


__all__ = [
    "CommitOp",
    "CommitSummaryOp",
    "DeleteItemOp",
    "DeleteReferenceOp",
    "DeltaApplier",
    "DeltaError",
    "DeltaOp",
    "DeltaParser",
    "DeltaResult",
    "DeltaStats",
    "ProgressCallback",
    "ProgressReporter",
    "ReferenceMeta",
    "SafeProgressReporter",
    "UpsertItemOp",
    "UpsertReferenceOp",
    "apply_delta",
    "iter_ops",
    "parse_op",
]
