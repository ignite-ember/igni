"""Wire-schema module — the ONLY place JSONL op shapes are defined.

Producers (ember-server) emit a JSONL file describing what changed
between the parent commit and the new one. Each line is a single JSON
object with an ``op`` field. The six op models below cover every
line kind the applier understands.

The :data:`DeltaOp` alias is a Pydantic ``Annotated[Union[...],
Field(discriminator='op')]`` tagged union — the parser drives a single
``TypeAdapter`` against this and Pydantic dispatches on ``op`` in one
shot. No module-level ``{name → model}`` registry, no two-step name
lookup, no isinstance-vs-dict inconsistency.

Behaviour that used to live in the applier's helper functions is
attached to the ops themselves:

- :meth:`UpsertItemOp.to_item` translates a wire payload to a
  :class:`CodeIndexItem` (was the 55-line ``_op_to_item`` free function).
- :meth:`CommitSummaryOp.write_project_map` persists the server-rendered
  markdown to disk (was inlined in the applier's op loop).

Categorical fields use ``Literal`` types where all producers agree on a
fixed vocabulary. ``kind`` stays ``Literal[...] | None`` because folder
ops legitimately omit it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.code_index.enums import FileSystemType
from ember_code.core.code_index.project_map import ProjectMap
from ember_code.core.code_index.schema.items import CodeIndexItem


class CommitOp(BaseModel):
    """First line of every JSONL delta — carries commit lineage.

    The applier calls ``prepare_commit(sha, parent_sha)`` before any
    data ops so the per-commit chroma directory exists (copy-on-write
    from ``parent_sha`` when present).
    """

    op: Literal["commit"]
    sha: str
    parent_sha: str | None = None
    branches: list[str] = Field(default_factory=list)
    indexed_at: str | None = None


class UpsertItemOp(BaseModel):
    """Insert or replace a file / folder / entity in the index.

    Mirrors ``ember-server/app/services/jsonl_changeset/writer.py``.
    Every quality dimension is independently optional so files /
    entities / folders only carry the dimensions that apply to them.

    ``id`` is a stable per-path identifier (``UUID5(path)``); the same
    path keeps the same id across commits, so a content change on an
    existing item replaces it in place rather than inserting an orphan.
    """

    op: Literal["upsert_item"]
    id: str
    # Constrained to the three concrete values every producer emits
    # (fixes the audit's stringly-typed-field violation).
    type: Literal["file", "folder", "entity"]
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

    # Code vs docs — the only place that distinction lives. Nullable
    # because folder ops legitimately omit it.
    kind: Literal["code", "docs"] | None = None

    # Entity classification (None for files / folders).
    entity_type: str | None = None

    # Quality categoricals — kept as free strings here because the wire
    # protocol is deliberately loose (producers emit `"unknown"` for
    # not-assessed items and the domain enums accept it). Tighten to
    # Literals only if every producer alignment is verified.
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

    # Mapping from the three wire ``type`` strings to the domain enum.
    # Kept as a ClassVar dict because the mapping is fully closed by
    # the ``Literal`` above and never mutates at runtime.
    _TYPE_MAP: ClassVar[dict[str, FileSystemType]] = {
        "folder": FileSystemType.FOLDER,
        "file": FileSystemType.FILE,
        "entity": FileSystemType.ENTITY,
    }

    def to_item(self) -> CodeIndexItem:
        """Translate this wire op into a domain :class:`CodeIndexItem`.

        The op's ``type`` string maps to the matching :class:`FileSystemType`
        member so the chroma metadata's ``type`` column carries the
        distinction (an entity never collides with a file at filter
        time). Every other field is a direct passthrough — the wire
        schema is a superset of the fields the domain model needs, so
        we build the payload explicitly rather than relying on
        ``model_dump()`` because the wire uses ``id`` while the domain
        uses ``item_id``.
        """
        return CodeIndexItem(
            item_id=self.id,
            name=self.name,
            type=self._TYPE_MAP[self.type],
            path=self.path,
            parent_id=self.parent_id,
            content=self.content,
            file_extension=self.file_extension,
            repository_id=self.repository_id,
            token_count=self.token_count,
            line_from=self.line_from,
            line_to=self.line_to,
            kind=self.kind,
            entity_type=self.entity_type,
            quality=self.quality,
            complexity=self.complexity,
            security=self.security,
            testing=self.testing,
            testability=self.testability,
            documentation=self.documentation,
            performance=self.performance,
            issues=self.issues,
            maintainability=self.maintainability,
            architecture=self.architecture,
            technical_debt=self.technical_debt,
            cohesion=self.cohesion,
            coupling=self.coupling,
            stability=self.stability,
            priority=self.priority,
            needs_refactoring=self.needs_refactoring,
            vulnerabilities=self.vulnerabilities,
            frameworks=self.frameworks,
            domain=self.domain,
            concerns=self.concerns,
            layers=self.layers,
            patterns=self.patterns,
            keywords=self.keywords,
            file_issues=self.file_issues,
        )


class DeleteItemOp(BaseModel):
    """Remove an item from the current commit's index."""

    op: Literal["delete_item"]
    id: str


class ReferenceMeta(BaseModel):
    """Free-form metadata payload on a reference edge.

    ``extra='allow'`` so callers can attach arbitrary keys
    (``{"line": 5}``, ``{"symbol": "foo"}``, …) without a producer
    migration. Concrete keys can be promoted to typed fields here as
    the schema stabilises.
    """

    model_config = ConfigDict(extra="allow")


class UpsertReferenceOp(BaseModel):
    """Insert or replace a reference edge in the per-project SQLite.

    References live outside the chroma commit scope — they persist
    until explicitly deleted (or cascaded when an endpoint item is
    deleted). ``relation`` is the canonical edge kind (``"calls"`` /
    ``"called_by"`` / ``"imports"`` / …).
    """

    op: Literal["upsert_reference"]
    from_id: str
    to_id: str
    relation: str
    meta: ReferenceMeta = Field(default_factory=ReferenceMeta)


class DeleteReferenceOp(BaseModel):
    """Remove a reference edge between two items."""

    op: Literal["delete_reference"]
    from_id: str
    to_id: str


class CommitSummaryOp(BaseModel):
    """Server-emitted commit-level project map.

    Carries the LLM-rendered markdown for the project map; the
    applier writes it to ``<chroma_dir>/../<sha>.project_map.md`` so
    the agent loads it at session start. Emitted by the server once
    per changeset, after all per-entity summaries are available — that
    way the server's summarizer model is a single source of truth
    rather than each client generating their own version.
    """

    op: Literal["commit_summary"]
    sha: str
    markdown: str

    def write_project_map(
        self,
        project: str | Path,
        data_dir: str | Path,
    ) -> Path:
        """Persist this commit's server-rendered map to disk.

        Moved off the applier so the op itself owns the ProjectMap
        interaction. Returns the written path so the caller can flag
        stats without knowing ProjectMap internals.
        """
        return ProjectMap(
            project=project,
            commit_sha=self.sha,
            data_dir=data_dir,
        ).write(self.markdown)


# Discriminated tagged union — Pydantic dispatches on the ``op`` field
# in one validation call. Replaces the previous ``_OP_MODELS`` dict +
# two-step name-then-model lookup with a single ``TypeAdapter`` in
# ``parser.py``.
DeltaOp = Annotated[
    Union[  # noqa: UP007 — required for Pydantic discriminated unions
        CommitOp,
        UpsertItemOp,
        DeleteItemOp,
        UpsertReferenceOp,
        DeleteReferenceOp,
        CommitSummaryOp,
    ],
    Field(discriminator="op"),
]
