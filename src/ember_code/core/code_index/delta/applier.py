"""Streaming applier — replays a JSONL changeset into the local index.

:class:`DeltaApplier` is the OOP-shaped replacement for the previous
free ``apply_delta`` function. The constructor takes the collaborators
(index, file_refs, jsonl_path, on_progress) as instance state and each
per-op method mutates ``self.stats`` / ``self._done`` in place — no
more free functions taking a state object as their first arg.

Op dispatch is a bound-method table (``self._handlers``) keyed on the
op *type*, not the op *name string* — this replaces the previous
six-branch isinstance chain (AP4 fix) and the parallel ``_OP_MODELS``
name-keyed dict (Pattern-2 fix). Adding a new op means adding one
model in ``ops.py``, one entry in the handlers table, and one method
here — no other file changes.

Progress-reporting safety is delegated to
:class:`~ember_code.core.code_index.delta.progress.SafeProgressReporter`
so the "never let progress break apply" invariant lives in one place
(was two duplicated try/except blocks in the old free function — AP6
fix).

Error contract:

- Truly unrecoverable JSONL malformation (invalid JSON, unknown op,
  validation failure, empty file, missing commit header, second
  commit header) raises :class:`DeltaError` — same as before, same
  message substrings, so existing tests don't churn.
- The public entry :meth:`run` returns a :class:`DeltaResult` on
  success carrying the final :class:`DeltaStats`. Migration of the
  raise paths to ``DeltaResult(ok=False, reason=...)`` is a deliberate
  follow-up — this refactor keeps the observable behaviour identical.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.code_index.delta.ops import (
    CommitOp,
    CommitSummaryOp,
    DeleteItemOp,
    DeleteReferenceOp,
    DeltaOp,
    UpsertItemOp,
    UpsertReferenceOp,
)
from ember_code.core.code_index.delta.parser import DeltaParser
from ember_code.core.code_index.delta.progress import (
    ProgressCallback,
    SafeProgressReporter,
)
from ember_code.core.code_index.delta.stats import (
    DeltaError,
    DeltaResult,
    DeltaStats,
)

if TYPE_CHECKING:
    from ember_code.core.code_index.db.file_reference import FileReferenceService
    from ember_code.core.code_index.index import CodeIndex

logger = logging.getLogger(__name__)


class DeltaApplier:
    """Streaming applier — one instance per JSONL file per :meth:`run` call.

    Instances are single-use: ``self.stats`` / ``self._done`` / ``self._sha``
    mutate as :meth:`run` progresses. Construct a new applier per file.

    ``on_progress`` (if provided) is invoked as items are applied. The
    callback receives ``(done, total, current_label)``: the number of
    ``upsert_item`` ops processed so far, the total predicted upserts,
    and a short text label for the most recent item (its path / name).
    Only ``upsert_item`` ops count toward the total — reference ops are
    cheap and don't move the bar. Used by ``/codeindex resync`` to
    drive the busy-label progress display.
    """

    def __init__(
        self,
        *,
        index: CodeIndex,
        file_refs: FileReferenceService,
        jsonl_path: str | Path,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self._index = index
        self._file_refs = file_refs
        self._jsonl_path = Path(jsonl_path)
        self._progress = SafeProgressReporter(on_progress)
        self._parser = DeltaParser()

        # Per-run mutable state — reset by construction, not by
        # ``run()``, so accidental re-runs surface as ``ValueError``
        # from ``prepare_commit`` rather than silent double-apply.
        self.stats = DeltaStats()
        self._done = 0
        self._sha: str | None = None
        self._total_items = 0

        # Bound-method dispatch table keyed on op type. Value type is
        # loosened to ``Callable[..., Awaitable[None]]`` because Python
        # ``Callable`` is contravariant in argument position — a
        # ``Callable[[UpsertItemOp], ...]`` is not assignable to a
        # ``Callable[[DeltaOp], ...]`` even though the runtime call is
        # safe (each key routes to the handler for its concrete op
        # type). Per-handler methods keep their narrow op-type
        # annotations, so callers still see the specific type.
        self._handlers: dict[type[DeltaOp], Callable[..., Awaitable[None]]] = {
            CommitOp: self._reject_second_commit_header,
            UpsertItemOp: self._apply_upsert_item,
            DeleteItemOp: self._apply_delete_item,
            UpsertReferenceOp: self._apply_upsert_reference,
            DeleteReferenceOp: self._apply_delete_reference,
            CommitSummaryOp: self._apply_commit_summary,
        }

    # -- Public entry ---------------------------------------------------------

    async def run(self) -> DeltaResult:
        """Stream the JSONL and apply each op to the index + reference table.

        Returns a :class:`DeltaResult` wrapping the accumulated
        :class:`DeltaStats`. Raises :class:`DeltaError` for
        unrecoverable JSONL malformation (see module docstring for
        the full list).
        """
        self._precount_items()
        ops_iter = self._parser.iter_ops(self._jsonl_path)
        await self._consume_commit_header(ops_iter)
        self._progress.report(0, self._total_items, "preparing")

        for op in ops_iter:
            handler = self._handlers.get(type(op))
            if handler is None:  # pragma: no cover — exhaustive over registered ops
                self.stats.skipped_lines += 1
                continue
            await handler(op)

        assert self._sha is not None  # narrowing for the type checker
        await self._index.set_head(self._sha)
        return DeltaResult(ok=True, stats=self.stats)

    # -- Setup ---------------------------------------------------------------

    def _precount_items(self) -> None:
        """Pre-pass: count expensive ops so the progress denominator is accurate.

        Cheap (microseconds for a ~30-line snapshot, milliseconds for
        the largest realistic delta) and skipped entirely when no
        callback is wired.
        """
        if not self._progress.enabled:
            return
        with self._jsonl_path.open() as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if obj.get("op") == "upsert_item":
                    self._total_items += 1

    async def _consume_commit_header(self, ops_iter: Iterator[DeltaOp]) -> None:
        """Pull the first op, verify it's a :class:`CommitOp`, and prepare the commit.

        Raises :class:`DeltaError` for the two malformation cases the
        applier can catch here: an empty file, or a first op that
        isn't ``commit``.
        """
        try:
            first = next(ops_iter)
        except StopIteration as exc:
            raise DeltaError("empty delta file") from exc
        if not isinstance(first, CommitOp):
            raise DeltaError(f"first line must be a 'commit' op, got {type(first).__name__}")
        self._sha = first.sha
        await self._index.prepare_commit(first.sha, parent_sha=first.parent_sha)

    # -- Per-op handlers -----------------------------------------------------

    async def _reject_second_commit_header(self, op: CommitOp) -> None:
        raise DeltaError(f"unexpected second commit header at sha={op.sha}")

    async def _apply_upsert_item(self, op: UpsertItemOp) -> None:
        assert self._sha is not None
        await self._index.add_item(self._sha, op.to_item())
        self.stats.items_upserted += 1
        self._done += 1
        self._progress.report(self._done, self._total_items, op.path or op.name or "")

    async def _apply_delete_item(self, op: DeleteItemOp) -> None:
        assert self._sha is not None
        await self._index.remove_item(self._sha, op.id)
        # Cascade — every reference (in or out) involving this UUID is
        # gone too. Without this the file_references table grows an
        # orphan row per deleted entity, which then shows up in
        # ``codeindex_tree`` / reverse-lookup results as edges pointing
        # nowhere. The cloud emitter doesn't send explicit
        # ``delete_reference`` ops for items it kills via
        # ``delete_item`` — it relies on this cascade.
        removed_refs = await self._file_refs.delete_by_uuid(op.id)
        self.stats.items_deleted += 1
        self.stats.references_deleted += removed_refs

    async def _apply_upsert_reference(self, op: UpsertReferenceOp) -> None:
        await self._file_refs.create(
            from_uuid=op.from_id,
            to_uuid=op.to_id,
            relation=op.relation,
            meta=op.meta,
        )
        self.stats.references_upserted += 1

    async def _apply_delete_reference(self, op: DeleteReferenceOp) -> None:
        await self._file_refs.delete(from_uuid=op.from_id, to_uuid=op.to_id)
        self.stats.references_deleted += 1

    async def _apply_commit_summary(self, op: CommitSummaryOp) -> None:
        # The server pre-rendered the project map for this commit;
        # persist it next to the chroma so the session loader can read
        # it without making any LLM calls of its own. All map
        # generation is now server-side; the client only persists what
        # arrives in the changeset.
        op.write_project_map(self._index.project, self._index.data_dir)
        self.stats.commit_summary_written = True
