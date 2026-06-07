"""Pydantic models for the codeindex toolkit.

Three groups:

  - **Output envelopes** — what the toolkit returns to the agent.
    :class:`ItemsResponse` is shared by both ``codeindex_query`` and
    ``codeindex_tree``; :class:`ErrorResponse` is the failure shape.
  - **Disambiguation refs** — the server-side reference re-ranking
    surfaced on ``codeindex_query`` responses to help the agent
    distinguish near-miss candidates.
  - **Internal filter envelopes** — :class:`_CategoricalFilters` and
    :class:`_ListFilters` carry the agent's structured query args from
    the toolkit method down to the where-builder and post-filter.

All ``_``-prefixed names are package-private; ``ItemsResponse`` and
``ErrorResponse`` are the only public schemas the agent sees.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ember_code.core.code_index.enums import (
    CohesionLevel,
    ComplexityLevel,
    CouplingLevel,
    DocumentationLevel,
    IssuesSeverity,
    Kind,
    PerformanceLevel,
    PriorityLevel,
    QualityLevel,
    SecurityLevel,
    StabilityLevel,
    TechnicalDebtLevel,
    TestabilityLevel,
    TestingLevel,
)
from ember_code.core.code_index.schema.items import CodeIndexResult

# ── Disambiguation refs ──────────────────────────────────────────────


class _DisambiguationRef(BaseModel):
    """A single reference target re-ranked by similarity to the caller's
    ``query_text``. Surfaced in :class:`ItemsResponse.refs` for the top
    result items so the agent can disambiguate near-miss candidates by
    looking at HOW each one is used in the codebase, not just by their
    summary text.
    """

    item_id: str
    name: str = ""
    path: str = ""
    summary: str = ""  # full LLM-generated content (multiple sections)
    score: float | None = None  # similarity to the original query_text


class _DisambiguationGroup(BaseModel):
    """Refs for one item, split by edge direction.

    Each list is already capped + ranked by similarity to the original
    query_text — the agent should read these as evidence about how the
    parent item is used in the codebase.

    ``via_parent`` is set when the refs come from a PARENT entity (the
    enclosing class or file) rather than the item itself. Triggered
    when the item has no direct CALLS / CALLED_BY edges of its own —
    common for indirectly-dispatched methods, methods only called by
    tests, or methods on classes accessed via factories. The string
    holds the parent's ``name (path)`` so the agent knows the
    relationship is one level up.
    """

    called_by: list[_DisambiguationRef] = Field(default_factory=list)
    calls: list[_DisambiguationRef] = Field(default_factory=list)
    via_parent: str | None = None


# ── Output envelopes ─────────────────────────────────────────────────


class _TreeNode(BaseModel):
    """One node in the structural tree returned by ``codeindex_query``.

    Each node represents one entity (folder / file / class / function /
    constant) along the chain from a matched leaf up to its immediate
    folder. The tree mirrors the codebase's structure: folder → file →
    class → entity. Summaries on intermediate nodes give the agent the
    surrounding context (what does this folder do, what's this file's
    convention, what does this class wrap) before it reaches the
    matched leaf.
    """

    item_id: str
    type: str = ""  # "folder" | "file" | "entity"
    entity_type: str = ""  # "class_definition" / "function_definition" / "" for files+folders
    name: str = ""
    path: str = ""
    line_from: int | None = None
    line_to: int | None = None
    score: float | None = None
    # ``summary`` is the rendered content for this node: the matched
    # sections for a leaf (full ``content``), or the one-line summary
    # for intermediate nodes (cheap context). Empty when the indexer
    # didn't produce a summary.
    summary: str = ""
    # Names of OTHER children under the same parent that didn't match.
    # Empty for the root nodes in a response.
    siblings: list[str] = Field(default_factory=list)
    # Recursive: the next-level matches under this node. ``[]`` means
    # this node is a leaf in the tree (no deeper match).
    matches: list[_TreeNode] = Field(default_factory=list)
    # Disambiguation refs (callers + callees re-ranked vs ``query_text``).
    # Only populated on entity-level leaves; ``None`` everywhere else
    # because folders/files/classes don't have call-graph edges.
    refs: _DisambiguationGroup | None = None
    # Full reference graph — populated only by ``codeindex_tree``.
    # Maps relation name (``calls``, ``called_by``, ``imports``,
    # ``imported_by``, ``extends``, ``extended_by`` …) to the
    # complete list of edges of that kind for this item. Distinct
    # from ``refs``: ``refs`` is a query-relevance-ranked subset
    # capped at top-K; ``references`` is unbounded and unranked.
    references: dict[str, list[Any]] | None = None


class ItemsResponse(BaseModel):
    """Response envelope for both ``codeindex_query`` and
    ``codeindex_tree``.

    ``items`` is a forest: each top-level entry is the highest-scoring
    folder (or file/entity at root scope), with its matched descendants
    nested as ``matches``. Disambiguation refs ride on the leaf
    entities, not on a separate top-level map.
    """

    commit: str
    items: list[_TreeNode]
    total: int
    truncated: bool = False


class ErrorResponse(BaseModel):
    error: str


# ── Internal filter envelopes ────────────────────────────────────────
#
# Two pydantic models hold the structured args while we move them
# from the tool's flat parameter list down to the chroma-side / Python-
# side filter logic. Splitting categorical from list-shaped here keeps
# the where-builder and the post-filter on opposite sides of a clear
# boundary: categoricals can be pushed down to chroma, list-shaped
# can't (chroma's metadata ``where`` lacks ``$contains``).


class _CategoricalFilters(BaseModel):
    """Single-value (or ``$in`` list) filters that push down to chroma.

    Every field is independently optional — ``None`` means "no filter
    on this dimension". A list value on any quality field means
    "match any of these values" (``$in``).
    """

    # Scope
    kind: Kind | None = None
    type: str | None = None
    entity_type: str | list[str] | None = None
    file_extension: str | None = None
    path_prefix: str | None = None
    needs_refactoring: bool | None = None

    # Quality categoricals
    quality: QualityLevel | list[QualityLevel] | None = None
    complexity: ComplexityLevel | list[ComplexityLevel] | None = None
    security: SecurityLevel | list[SecurityLevel] | None = None
    testing: TestingLevel | list[TestingLevel] | None = None
    testability: TestabilityLevel | list[TestabilityLevel] | None = None
    documentation: DocumentationLevel | list[DocumentationLevel] | None = None
    performance: PerformanceLevel | list[PerformanceLevel] | None = None
    issues: IssuesSeverity | list[IssuesSeverity] | None = None
    maintainability: QualityLevel | list[QualityLevel] | None = None
    architecture: QualityLevel | list[QualityLevel] | None = None
    technical_debt: TechnicalDebtLevel | list[TechnicalDebtLevel] | None = None
    cohesion: CohesionLevel | list[CohesionLevel] | None = None
    coupling: CouplingLevel | list[CouplingLevel] | None = None
    stability: StabilityLevel | list[StabilityLevel] | None = None
    priority: PriorityLevel | list[PriorityLevel] | None = None


# Names of the quality categoricals — used by the where-builder loop
# so adding a new dimension doesn't require a second hand-edit. Scope
# and ``needs_refactoring`` have field-specific shape so they're
# handled explicitly outside this list.
_CATEGORICAL_QUALITY_FIELDS: tuple[str, ...] = (
    "quality",
    "complexity",
    "security",
    "testing",
    "testability",
    "documentation",
    "performance",
    "issues",
    "maintainability",
    "architecture",
    "technical_debt",
    "cohesion",
    "coupling",
    "stability",
    "priority",
)


class _ListFilters(BaseModel):
    """Multi-value categories. Applied as a Python post-filter after
    chroma narrows on categoricals — chroma metadata ``where`` has no
    ``$contains`` operator, and exploding to one row per value would
    triple the index.
    """

    vulnerabilities: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    file_issues: list[str] = Field(default_factory=list)

    @property
    def has_any(self) -> bool:
        """True iff any list category carries at least one value."""
        return any(getattr(self, f) for f in type(self).model_fields)

    def matches(self, item: CodeIndexResult) -> bool:
        """True iff ``item`` matches every non-empty list filter.

        Cross-category is AND (every filter that's set must hit); within
        one category any value matching the item's list counts as a hit
        (OR within).
        """
        for field in type(self).model_fields:
            wanted = getattr(self, field)
            if not wanted:
                continue
            present = set(getattr(item, field, []) or [])
            if not present.intersection(wanted):
                return False
        return True
