"""Pydantic-driven JSONL changeset builder for the CodeIndex eval.

Bypasses the server's LLM summarization pipeline. The fixture declares
items + their quality flags directly; this module emits the JSONL the
client's ``apply_delta`` would normally pull from GCS.

Why this lives here, not in tests/: the spec is shared between the
plumbing test (no agent) and the eval setup hook (full agent run), so
it has to be importable from both.

The shape of a fixture mirrors what the server emits:

- folders depth-first
- files (code or docs)
- entities (functions / classes / sections)
- references (call graph) — emitted in both directions

Content shape mirrors what the server's emitter produces:

- File row content = the LLM file summary (seven structured sections:
  ``purpose_and_functionality``, ``architecture_and_design``,
  ``code_quality``, ``security``, ``issues_and_technical_debt``,
  ``testing_and_reliability``, ``dependencies_and_impact``, plus
  optional ``recommendations``) followed by a generated
  ``[SECTION:entities]`` listing — matches
  ``app/services/jsonl_changeset/emitter.py::_format_file_summary``.
- Entity row content = the LLM entity summary (mandatory ``summary``
  section plus optional ``quality_assessment`` / ``security_analysis``
  / ``issues_and_concerns`` / ``testing_status`` sections) — matches
  ``app/dataset/dataset_creation/schemas/processed_entity.py``.

Stable identity: every item id is ``uuid5(NS, path)`` so re-runs of
the eval against the same fixture produce the same chroma rows. That
keeps embeddings cache-warm across iterations.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ember_code.core.code_index.delta import (
    CommitOp,
    UpsertItemOp,
    UpsertReferenceOp,
)
from ember_code.core.code_index.enums import (
    CohesionLevel,
    ComplexityLevel,
    CouplingLevel,
    DocumentationLevel,
    IssuesSeverity,
    PerformanceLevel,
    PriorityLevel,
    QualityLevel,
    SecurityLevel,
    StabilityLevel,
    TechnicalDebtLevel,
    TestabilityLevel,
    TestingLevel,
)

# Fixed UUID5 namespace so a given fixture always produces the same ids.
_NAMESPACE = uuid.UUID("8c3f0f3c-5f91-4a7f-b5d3-2c0e6f9c1a01")


def _uuid_for(path: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, path))


# ── Spec models ──────────────────────────────────────────────────────


class _QualityMixin(BaseModel):
    """Optional quality + category fields shared by file / entity specs.

    Stays compatible with the writer's ``UpsertItemOp`` field set so a
    spec field maps 1:1 to the chroma column.
    """

    quality: QualityLevel | None = None
    complexity: ComplexityLevel | None = None
    security: SecurityLevel | None = None
    testing: TestingLevel | None = None
    testability: TestabilityLevel | None = None
    documentation: DocumentationLevel | None = None
    performance: PerformanceLevel | None = None
    issues: IssuesSeverity | None = None
    maintainability: QualityLevel | None = None
    architecture: QualityLevel | None = None
    technical_debt: TechnicalDebtLevel | None = None
    cohesion: CohesionLevel | None = None
    coupling: CouplingLevel | None = None
    stability: StabilityLevel | None = None
    priority: PriorityLevel | None = None
    needs_refactoring: bool | None = None

    vulnerabilities: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    file_issues: list[str] = Field(default_factory=list)


class EntitySummary(BaseModel):
    """Mirrors the server-side per-entity summary shape.

    Server: see ``processed_entity.py::_build_entity_summary_text``.
    The ``summary`` field is mandatory; the other four are emitted
    only when truthy — same conditional as the server.
    """

    summary: str
    quality_assessment: str = ""
    security_analysis: str = ""
    issues_and_concerns: str = ""
    testing_status: str = ""

    def to_text(self) -> str:
        out = f"[SECTION:summary]\n{self.summary}\n[/SECTION]"
        if self.quality_assessment:
            out += f"\n\n[SECTION:quality_assessment]\n{self.quality_assessment}\n[/SECTION]"
        if self.security_analysis:
            out += f"\n\n[SECTION:security_analysis]\n{self.security_analysis}\n[/SECTION]"
        if self.issues_and_concerns:
            out += f"\n\n[SECTION:issues_and_concerns]\n{self.issues_and_concerns}\n[/SECTION]"
        if self.testing_status:
            out += f"\n\n[SECTION:testing_status]\n{self.testing_status}\n[/SECTION]"
        return out


class FileSummary(BaseModel):
    """Mirrors the server-side per-file summary shape.

    Server: see ``processed_file.py::_build_final_file_summary_text``.
    Seven sections are emitted unconditionally (empty content allowed
    between markers); ``recommendations`` is appended only when set.
    The emitter then tacks on a generated ``[SECTION:entities]`` block —
    we do the same in :func:`build_changeset` so each file row carries
    its entity listing.
    """

    purpose_and_functionality: str
    architecture_and_design: str = ""
    code_quality: str = ""
    security: str = ""
    issues_and_technical_debt: str = ""
    testing_and_reliability: str = ""
    dependencies_and_impact: str = ""
    recommendations: str | None = None

    def to_text(self) -> str:
        out = (
            f"[SECTION:purpose_and_functionality]\n{self.purpose_and_functionality}\n[/SECTION]"
            f"\n\n[SECTION:architecture_and_design]\n{self.architecture_and_design}\n[/SECTION]"
            f"\n\n[SECTION:code_quality]\n{self.code_quality}\n[/SECTION]"
            f"\n\n[SECTION:security]\n{self.security}\n[/SECTION]"
            f"\n\n[SECTION:issues_and_technical_debt]\n{self.issues_and_technical_debt}\n[/SECTION]"
            f"\n\n[SECTION:testing_and_reliability]\n{self.testing_and_reliability}\n[/SECTION]"
            f"\n\n[SECTION:dependencies_and_impact]\n{self.dependencies_and_impact}\n[/SECTION]"
        )
        if self.recommendations:
            out += f"\n\n[SECTION:recommendations]\n{self.recommendations}\n[/SECTION]"
        return out


class FixtureEntity(_QualityMixin):
    """One function/class/method inside a code file."""

    name: str
    entity_type: str  # "function" | "class" | "method"
    line_from: int
    line_to: int
    content: EntitySummary
    # Outgoing call edges. Each entry is a ``"<file_path>::<entity_name>"``
    # pointer to another entity in the fixture. Resolved to UUIDs by the
    # builder; unresolved targets are dropped (mirrors the server's
    # behavior when a call lands on something it didn't index).
    calls: list[str] = Field(default_factory=list)


class FixtureFile(_QualityMixin):
    """One source file (code, NOT docs)."""

    path: str
    content: FileSummary
    file_extension: str = ".py"
    entities: list[FixtureEntity] = Field(default_factory=list)
    # ``imports`` are file-to-file edges, separate from entity-level calls.
    imports: list[str] = Field(default_factory=list)


class FixtureDocSection(BaseModel):
    """One markdown section inside a doc file."""

    name: str
    level: int
    line_from: int
    line_to: int
    body: str
    parent_chain: list[str] = Field(default_factory=list)


class FixtureDocFile(BaseModel):
    """One markdown file plus its parsed sections."""

    path: str
    body: str
    file_extension: str = ".md"
    sections: list[FixtureDocSection] = Field(default_factory=list)


class FixtureRepo(BaseModel):
    """Top-level fixture spec.

    Folders are listed explicitly so the emitter can order them
    parent-before-child. The builder doesn't infer them from file paths
    because the order matters for ``parent_id`` resolution.
    """

    folders: list[str] = Field(default_factory=list)
    files: list[FixtureFile] = Field(default_factory=list)
    docs: list[FixtureDocFile] = Field(default_factory=list)


# ── Builder ──────────────────────────────────────────────────────────


def _quality_kwargs(spec: _QualityMixin) -> dict:
    """Pull quality + list fields off a spec into kwargs for ``UpsertItemOp``.

    Empty defaults stay empty (lists default to ``[]``); ``None``
    categoricals are passed through so ``_drop_none=True`` on the op
    keeps them out of the JSONL line.
    """
    return {
        "quality": _enum(spec.quality),
        "complexity": _enum(spec.complexity),
        "security": _enum(spec.security),
        "testing": _enum(spec.testing),
        "testability": _enum(spec.testability),
        "documentation": _enum(spec.documentation),
        "performance": _enum(spec.performance),
        "issues": _enum(spec.issues),
        "maintainability": _enum(spec.maintainability),
        "architecture": _enum(spec.architecture),
        "technical_debt": _enum(spec.technical_debt),
        "cohesion": _enum(spec.cohesion),
        "coupling": _enum(spec.coupling),
        "stability": _enum(spec.stability),
        "priority": _enum(spec.priority),
        "needs_refactoring": spec.needs_refactoring,
        "vulnerabilities": list(spec.vulnerabilities),
        "frameworks": list(spec.frameworks),
        "domain": list(spec.domain),
        "concerns": list(spec.concerns),
        "layers": list(spec.layers),
        "patterns": list(spec.patterns),
        "keywords": list(spec.keywords),
        "file_issues": list(spec.file_issues),
    }


def _enum(value):
    """StrEnum → its string value, or ``None`` passthrough."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else value


def build_changeset(repo: FixtureRepo, *, commit_sha: str) -> list[BaseModel]:
    """Produce the ordered list of ops for ``apply_delta``.

    Order matches what the server emits: ``commit`` → folders (parent
    first) → files → entities → references.
    """
    ops: list[BaseModel] = [
        CommitOp(op="commit", sha=commit_sha, parent_sha=None, branches=["main"]),
    ]

    # Folders — parent before child via path-depth sort.
    folder_ids: dict[str, str] = {}
    for folder_path in sorted(repo.folders, key=lambda p: p.count("/")):
        folder_id = _uuid_for(folder_path)
        folder_ids[folder_path] = folder_id
        parent_path = _parent_folder(folder_path)
        ops.append(
            UpsertItemOp(
                op="upsert_item",
                id=folder_id,
                type="folder",
                name=folder_path.rsplit("/", 1)[-1] or folder_path,
                path=folder_path,
                parent_id=folder_ids.get(parent_path),
                content=f"Folder: {folder_path}",
                kind="code",
            )
        )

    # Code files + entities.
    file_ids: dict[str, str] = {}
    entity_ids: dict[str, str] = {}
    for file in repo.files:
        file_id = _uuid_for(file.path)
        file_ids[file.path] = file_id
        # Match the server's emitter: structured summary + appended
        # ``[SECTION:entities]`` listing of the file's entities.
        entity_lines = "\n".join(
            f"- {file.path}::{e.name} ({e.entity_type})" for e in file.entities
        )
        file_content = (
            f"{file.content.to_text()}\n\n[SECTION:entities]\n{entity_lines}\n[/SECTION]\n"
        )
        ops.append(
            UpsertItemOp(
                op="upsert_item",
                id=file_id,
                type="file",
                name=Path(file.path).name,
                path=file.path,
                parent_id=folder_ids.get(_parent_folder(file.path)),
                content=file_content,
                kind="code",
                file_extension=file.file_extension,
                **_quality_kwargs(file),
            )
        )

    # Pre-allocate entity ids so call-graph references can resolve in one pass.
    for file in repo.files:
        for entity in file.entities:
            ent_path = f"{file.path}::{entity.name}"
            entity_ids[ent_path] = _uuid_for(ent_path)

    for file in repo.files:
        for entity in file.entities:
            ent_path = f"{file.path}::{entity.name}"
            ops.append(
                UpsertItemOp(
                    op="upsert_item",
                    id=entity_ids[ent_path],
                    type="entity",
                    name=entity.name,
                    path=ent_path,
                    parent_id=file_ids[file.path],
                    content=entity.content.to_text(),
                    kind="code",
                    file_extension=file.file_extension,
                    entity_type=entity.entity_type,
                    line_from=entity.line_from,
                    line_to=entity.line_to,
                    **_quality_kwargs(entity),
                )
            )

    # Doc files + sections (sections under their parent doc).
    section_ids: dict[str, str] = {}
    for doc in repo.docs:
        doc_id = _uuid_for(doc.path)
        file_ids[doc.path] = doc_id
        ops.append(
            UpsertItemOp(
                op="upsert_item",
                id=doc_id,
                type="file",
                name=Path(doc.path).name,
                path=doc.path,
                parent_id=folder_ids.get(_parent_folder(doc.path)),
                content=doc.body,
                kind="docs",
                file_extension=doc.file_extension,
            )
        )
        for section in doc.sections:
            chain = [*section.parent_chain, section.name]
            sec_path = f"{doc.path}::{'::'.join(chain)}"
            sec_id = _uuid_for(sec_path)
            section_ids[sec_path] = sec_id
            parent_chain_path = (
                f"{doc.path}::{'::'.join(section.parent_chain)}"
                if section.parent_chain
                else None
            )
            parent_id = section_ids.get(parent_chain_path) if parent_chain_path else doc_id
            ops.append(
                UpsertItemOp(
                    op="upsert_item",
                    id=sec_id,
                    type="entity",
                    name=section.name,
                    path=sec_path,
                    parent_id=parent_id,
                    content=section.body,
                    kind="docs",
                    file_extension=doc.file_extension,
                    entity_type="section",
                    line_from=section.line_from,
                    line_to=section.line_to,
                )
            )

    # References — calls (entity → entity) and imports (file → file).
    for file in repo.files:
        # File-level imports.
        for imported_path in file.imports:
            target_id = file_ids.get(imported_path)
            if not target_id:
                continue
            ops.extend(
                _mirror_ref(
                    file_ids[file.path],
                    target_id,
                    forward="imports",
                    reverse="imported_by",
                    forward_meta={"source_file": file.path, "target_file": imported_path},
                    reverse_meta={"importer_file": file.path, "imported_file": imported_path},
                )
            )
        # Entity-level calls.
        for entity in file.entities:
            caller_id = entity_ids[f"{file.path}::{entity.name}"]
            for callee in entity.calls:
                callee_id = entity_ids.get(callee)
                if not callee_id:
                    continue
                ops.extend(
                    _mirror_ref(
                        caller_id,
                        callee_id,
                        forward="calls",
                        reverse="called_by",
                        forward_meta={
                            "from_entity_path": f"{file.path}::{entity.name}",
                            "to_entity_path": callee,
                        },
                        reverse_meta={
                            "from_entity_path": callee,
                            "to_entity_path": f"{file.path}::{entity.name}",
                        },
                    )
                )
    return ops


def write_jsonl(ops: list[BaseModel], output_path: Path) -> Path:
    """Serialize an op list to JSONL on disk. Returns the path written.

    Mirrors the server writer's per-op exclusion policy: ``CommitOp``
    keeps ``parent_sha: null`` (it identifies a root commit), every
    other op drops Nones so optional fields only appear when set.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        for op in ops:
            exclude_none = not isinstance(op, CommitOp)
            fh.write(op.model_dump_json(exclude_none=exclude_none) + "\n")
    return output_path


def build_and_write(repo: FixtureRepo, *, commit_sha: str, output_path: Path) -> Path:
    return write_jsonl(build_changeset(repo, commit_sha=commit_sha), output_path)


# ── Internals ────────────────────────────────────────────────────────


def _parent_folder(path: str) -> str:
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _mirror_ref(
    from_id: str,
    to_id: str,
    *,
    forward: str,
    reverse: str,
    forward_meta: dict,
    reverse_meta: dict,
) -> list[UpsertReferenceOp]:
    return [
        UpsertReferenceOp(
            op="upsert_reference",
            from_id=from_id,
            to_id=to_id,
            relation=forward,
            meta=forward_meta,
        ),
        UpsertReferenceOp(
            op="upsert_reference",
            from_id=to_id,
            to_id=from_id,
            relation=reverse,
            meta=reverse_meta,
        ),
    ]


# Re-export for callers that just want the raw helper.
__all__ = [
    "FixtureRepo",
    "FixtureFile",
    "FixtureEntity",
    "FileSummary",
    "EntitySummary",
    "FixtureDocFile",
    "FixtureDocSection",
    "build_changeset",
    "build_and_write",
    "write_jsonl",
]


# Indicate target module for import-as-script users.
_MODULE_TYPE: Literal["fixture-builder"] = "fixture-builder"
