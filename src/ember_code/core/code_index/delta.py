"""JSONL delta contract + applier for the per-commit code index.

Producers (ember-server) emit a JSONL file describing what changed
between the parent commit and the new one. Each line is a single JSON
object with an ``op`` field. ``apply_delta`` streams the file and
mutates the local chroma index + SQLite reference table accordingly.

Contract — one object per line:

- ``{"op": "commit", "sha": "...", "parent_sha": "...|null", ...}``
  Always the first line. Carries lineage so the applier can
  ``prepare_commit(sha, parent_sha)`` before any data ops.
- ``{"op": "upsert_item", "id": "...", "type": "file|folder|entity", ...}``
  Insert or replace an item. ``id`` is the producer's stable content
  hash (UUID5 of path+content); unchanged items keep their id across
  commits. The full quality and category schema travels on this op —
  see ``UpsertItemOp`` below for every field.
- ``{"op": "delete_item", "id": "..."}`` — remove an item.
- ``{"op": "upsert_reference", "from_id": "...", "to_id": "...", "relation": "...", "meta": {}}``
  Insert or replace a reference. ``relation`` is the canonical edge
  kind ("calls" / "called_by" / "imports" / ...). References live in
  the per-project SQLite (no commit scope) — they persist until
  explicitly deleted.
- ``{"op": "delete_reference", "from_id": "...", "to_id": "..."}``

Idempotent: applying the same JSONL twice yields the same state. Safe
to retry on partial failure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ember_code.core.code_index.enums import FileSystemType
from ember_code.core.code_index.schema.items import CodeIndexItem

if TYPE_CHECKING:
    from ember_code.core.code_index.index import CodeIndex
    from ember_code.core.code_index.pg.file_reference import FileReferenceService

logger = logging.getLogger(__name__)


# -- Op schemas ---------------------------------------------------------------


class CommitOp(BaseModel):
    op: Literal["commit"]
    sha: str
    parent_sha: str | None = None
    branches: list[str] = Field(default_factory=list)
    indexed_at: str | None = None


class UpsertItemOp(BaseModel):
    """Mirrors ``ember-server/app/services/jsonl_changeset/writer.py``.

    Every quality dimension is independently optional so files /
    entities / folders only carry the dimensions that apply to them.
    """

    op: Literal["upsert_item"]
    id: str
    type: str  # "file" | "folder" | "entity"
    name: str
    content: str = ""

    # Structural / scope
    path: str | None = None
    parent_id: str | None = None
    file_extension: str | None = None
    repository_id: str | None = None
    token_count: int | None = None
    line_from: int | None = None
    line_to: int | None = None

    # Code vs docs — the only place that distinction lives.
    kind: str | None = None

    # Entity classification (None for files / folders).
    entity_type: str | None = None

    # Quality categoricals.
    quality: str | None = None
    complexity: str | None = None
    security: str | None = None
    testing: str | None = None
    testability: str | None = None
    documentation: str | None = None
    performance: str | None = None
    issues: str | None = None
    maintainability: str | None = None
    architecture: str | None = None
    technical_debt: str | None = None
    cohesion: str | None = None
    coupling: str | None = None
    stability: str | None = None
    priority: str | None = None
    needs_refactoring: bool | None = None

    # Multi-value categories.
    vulnerabilities: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    file_issues: list[str] = Field(default_factory=list)


class DeleteItemOp(BaseModel):
    op: Literal["delete_item"]
    id: str


class UpsertReferenceOp(BaseModel):
    op: Literal["upsert_reference"]
    from_id: str
    to_id: str
    relation: str
    meta: dict[str, Any] = Field(default_factory=dict)


class DeleteReferenceOp(BaseModel):
    op: Literal["delete_reference"]
    from_id: str
    to_id: str


class CommitSummaryOp(BaseModel):
    """Server-emitted commit-level project map.

    Carries the LLM-rendered markdown for the project map; the applier
    writes it to ``<chroma_dir>/../<sha>.project_map.md`` so the agent
    loads it at session start. Emitted by the server once per
    changeset, after all per-entity summaries are available — that
    way the server's summarizer model is a single source of truth
    rather than each client generating their own version.
    """

    op: Literal["commit_summary"]
    sha: str
    markdown: str


_OP_MODELS: dict[str, type[BaseModel]] = {
    "commit": CommitOp,
    "upsert_item": UpsertItemOp,
    "delete_item": DeleteItemOp,
    "upsert_reference": UpsertReferenceOp,
    "delete_reference": DeleteReferenceOp,
    "commit_summary": CommitSummaryOp,
}


@dataclass
class DeltaStats:
    items_upserted: int = 0
    items_deleted: int = 0
    references_upserted: int = 0
    references_deleted: int = 0
    skipped_lines: int = 0
    commit_summary_written: bool = False


class DeltaError(Exception):
    """Raised when the JSONL is malformed in a way the applier can't recover from."""


# -- Parsing ------------------------------------------------------------------


def parse_op(raw: str) -> BaseModel | None:
    """Parse one JSONL line into the matching op model, or ``None`` for blanks."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeltaError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "op" not in payload:
        raise DeltaError(f"missing 'op' field: {raw[:120]}")
    op_name = payload["op"]
    model = _OP_MODELS.get(op_name)
    if model is None:
        raise DeltaError(f"unknown op: {op_name!r}")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise DeltaError(f"validation failed for {op_name}: {exc}") from exc


def iter_ops(jsonl_path: str | Path):
    """Yield parsed ops from a JSONL file, skipping blank lines."""
    path = Path(str(jsonl_path)).expanduser()
    with path.open() as fh:
        for line_no, line in enumerate(fh, start=1):
            try:
                op = parse_op(line)
            except DeltaError as exc:
                raise DeltaError(f"line {line_no}: {exc}") from exc
            if op is not None:
                yield op


# -- Applier ------------------------------------------------------------------


async def apply_delta(
    *,
    index: CodeIndex,
    file_refs: FileReferenceService,
    jsonl_path: str | Path,
) -> DeltaStats:
    """Stream a JSONL file and apply each op to the index + reference table."""
    stats = DeltaStats()
    ops_iter = iter_ops(jsonl_path)

    try:
        first = next(ops_iter)
    except StopIteration as exc:
        raise DeltaError("empty delta file") from exc
    if not isinstance(first, CommitOp):
        raise DeltaError(f"first line must be a 'commit' op, got {type(first).__name__}")
    sha = first.sha
    await index.prepare_commit(sha, parent_sha=first.parent_sha)

    for op in ops_iter:
        if isinstance(op, CommitOp):
            raise DeltaError(f"unexpected second commit header at sha={op.sha}")
        elif isinstance(op, UpsertItemOp):
            await index.add_item(sha, _op_to_item(op))
            stats.items_upserted += 1
        elif isinstance(op, DeleteItemOp):
            await index.remove_item(sha, op.id)
            # Cascade — every reference (in or out) involving this UUID
            # is gone too. Without this the file_references table grows
            # an orphan row per deleted entity, which then shows up in
            # ``codeindex_tree`` / reverse-lookup results as edges
            # pointing nowhere. The cloud emitter doesn't send explicit
            # ``delete_reference`` ops for items it kills via
            # ``delete_item`` — it relies on this cascade.
            removed_refs = await file_refs.delete_by_uuid(op.id)
            stats.items_deleted += 1
            stats.references_deleted += removed_refs
        elif isinstance(op, UpsertReferenceOp):
            await file_refs.create(
                from_uuid=op.from_id,
                to_uuid=op.to_id,
                relation=op.relation,
                meta=op.meta,
            )
            stats.references_upserted += 1
        elif isinstance(op, DeleteReferenceOp):
            await file_refs.delete(from_uuid=op.from_id, to_uuid=op.to_id)
            stats.references_deleted += 1
        elif isinstance(op, CommitSummaryOp):
            # The server pre-rendered the project map for this commit;
            # write it to disk next to the chroma so the session
            # loader can read it without making any LLM calls of its
            # own. All map generation is now server-side; the client
            # only persists what arrives in the changeset.
            from ember_code.core.code_index.project_map import write_server_supplied_map

            write_server_supplied_map(
                project=index.project,
                data_dir=index.data_dir,
                commit_sha=op.sha,
                markdown=op.markdown,
            )
            stats.commit_summary_written = True
        else:  # pragma: no cover — exhaustive over registered ops
            stats.skipped_lines += 1

    await index.set_head(sha)
    return stats


def _op_to_item(op: UpsertItemOp) -> CodeIndexItem:
    """Translate a JSONL ``upsert_item`` payload to a :class:`CodeIndexItem`.

    The op's ``type`` is one of ``"folder"`` / ``"file"`` / ``"entity"``;
    that string maps to the matching :class:`FileSystemType` member so
    the chroma metadata's ``type`` column carries the distinction (an
    entity never collides with a file at filter time).
    """
    item_type_map = {
        "folder": FileSystemType.FOLDER,
        "file": FileSystemType.FILE,
        "entity": FileSystemType.ENTITY,
    }
    item_type = item_type_map.get(op.type, FileSystemType.FILE)

    return CodeIndexItem(
        item_id=op.id,
        name=op.name,
        type=item_type,
        path=op.path,
        parent_id=op.parent_id,
        content=op.content,
        file_extension=op.file_extension,
        repository_id=op.repository_id,
        token_count=op.token_count,
        line_from=op.line_from,
        line_to=op.line_to,
        kind=op.kind,
        entity_type=op.entity_type,
        quality=op.quality,
        complexity=op.complexity,
        security=op.security,
        testing=op.testing,
        testability=op.testability,
        documentation=op.documentation,
        performance=op.performance,
        issues=op.issues,
        maintainability=op.maintainability,
        architecture=op.architecture,
        technical_debt=op.technical_debt,
        cohesion=op.cohesion,
        coupling=op.coupling,
        stability=op.stability,
        priority=op.priority,
        needs_refactoring=op.needs_refactoring,
        vulnerabilities=op.vulnerabilities,
        frameworks=op.frameworks,
        domain=op.domain,
        concerns=op.concerns,
        layers=op.layers,
        patterns=op.patterns,
        keywords=op.keywords,
        file_issues=op.file_issues,
    )
