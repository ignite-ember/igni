"""Pydantic models for the codeindex toolkit.

Five groups:

  - **Output envelopes** — what the toolkit returns to the agent.
    :class:`ItemsResponse` is shared by both ``codeindex_query`` and
    ``codeindex_tree``; :class:`ErrorResponse` is the failure shape.
  - **Disambiguation refs** — the server-side reference re-ranking
    surfaced on ``codeindex_query`` responses to help the agent
    distinguish near-miss candidates.
  - **Internal filter envelopes** — :class:`_CategoricalFilters` and
    :class:`_ListFilters` carry the agent's structured query args from
    the toolkit method down to the where-builder and post-filter.
  - **Query + tree input** — :class:`QueryInput` bundles the full
    ``codeindex_query`` arg surface so the service doesn't accept 34
    kwargs. Owns validation, empty-call detection, and filter-envelope
    splitting. :class:`TreeInput` does the same for ``codeindex_tree``.
    Both expose ``from_tool_kwargs`` classmethods so the toolkit
    stops re-listing every field name. :class:`RenderedRow` wraps a
    :class:`CodeIndexResult` with its pre/post section-filter content
    so the tree builder consumes typed rows instead of reaching for
    sidecar attributes.
  - **Telemetry** — :class:`TelemetryEntry` is the typed record
    :class:`TelemetryLog` writes to disk, replacing the two raw-dict
    literals the old toolkit built inline.

All ``_``-prefixed names are package-private; ``ItemsResponse`` and
``ErrorResponse`` are the only public schemas the agent sees.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    Relation,
    Section,
    SecurityLevel,
    StabilityLevel,
    TechnicalDebtLevel,
    TestabilityLevel,
    TestingLevel,
)
from ember_code.core.code_index.schema.items import CodeIndexResult
from ember_code.core.code_index.schema.where_filter import ChromaWhereFilter

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

    @classmethod
    def from_flat_args(cls, params: QueryInput) -> _CategoricalFilters:
        """Build a categorical envelope from a :class:`QueryInput`.

        Keeps envelope construction next to the envelope definition
        instead of scattering it inside the service.
        """
        return cls(
            kind=params.kind,
            type=params.type,
            entity_type=params.entity_type,
            file_extension=params.file_extension,
            path_prefix=params.path_prefix,
            quality=params.quality,
            complexity=params.complexity,
            security=params.security,
            testing=params.testing,
            testability=params.testability,
            documentation=params.documentation,
            performance=params.performance,
            issues=params.issues,
            maintainability=params.maintainability,
            architecture=params.architecture,
            technical_debt=params.technical_debt,
            cohesion=params.cohesion,
            coupling=params.coupling,
            stability=params.stability,
            priority=params.priority,
            needs_refactoring=params.needs_refactoring,
        )

    def to_where(self) -> ChromaWhereFilter | None:
        """Translate this envelope into a :class:`ChromaWhereFilter`.

        Every non-``None`` field becomes one clause; multiple clauses
        combine under a top-level ``$and`` at the chroma boundary.
        Single values become direct equality, lists become ``$in``.

        List-shaped multi-value categories live on :class:`_ListFilters`
        and are applied Python-side — chroma metadata ``where`` lacks a
        ``$contains`` operator, so they can't be pushed down here.

        Returns ``None`` when no filters were supplied so the index code
        skips the where-clause entirely (chroma rejects ``where={}``).

        Mirrors the sibling :meth:`_ListFilters.matches` pattern — both
        envelopes own the translation of their fields into the shape
        the downstream layer needs, so the service stays out of the
        filter-shape business.
        """
        where = ChromaWhereFilter()
        any_set = False

        # Direct exact-match scope filters. StrEnum values render as
        # their plain string via ``str(v)`` — no defensive helper
        # needed because every field is already typed as StrEnum (or
        # list-of-StrEnum) at the Pydantic layer.
        if self.kind is not None:
            where.equals["kind"] = str(self.kind)
            any_set = True
        if self.type is not None:
            where.equals["type"] = self.type
            any_set = True
        if self.file_extension is not None:
            where.equals["file_extension"] = self.file_extension
            any_set = True
        # ``path_prefix`` is reserved — chroma metadata where has no
        # $contains/prefix operator, so we accept the arg and ignore it
        # rather than silently emit a broken filter. Re-enable once
        # there's a where-document-based path matcher.

        # ``entity_type`` — single value or list.
        if self.entity_type is not None:
            if isinstance(self.entity_type, list):
                where.in_["entity_type"] = [str(x) for x in self.entity_type]
            else:
                where.equals["entity_type"] = str(self.entity_type)
            any_set = True

        # ``needs_refactoring`` is bool.
        if self.needs_refactoring is not None:
            where.equals["needs_refactoring"] = bool(self.needs_refactoring)
            any_set = True

        # Quality categoricals.
        for field in _CATEGORICAL_QUALITY_FIELDS:
            v = getattr(self, field)
            if v is None:
                continue
            if isinstance(v, list):
                values = [str(x) for x in v if x is not None]
                if not values:
                    continue
                if len(values) == 1:
                    where.equals[field] = values[0]
                else:
                    where.in_[field] = values
            else:
                where.equals[field] = str(v)
            any_set = True

        if not any_set:
            return None
        return where


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

    @classmethod
    def from_flat_args(cls, params: QueryInput) -> _ListFilters:
        """Build a list-filter envelope from a :class:`QueryInput`.

        ``None`` gets normalised to ``[]`` here so downstream code can
        assume every field is an actual list.
        """
        return cls(
            vulnerabilities=params.vulnerabilities or [],
            frameworks=params.frameworks or [],
            domain=params.domain or [],
            concerns=params.concerns or [],
            layers=params.layers or [],
            patterns=params.patterns or [],
            keywords=params.keywords or [],
            file_issues=params.file_issues or [],
        )


# ── Query input + rendered-row wrapper ───────────────────────────────


# Field names on :class:`QueryInput` that carry NARROWING intent for the
# empty-call detector. Output-control (``sections`` / ``limit`` /
# ``commit`` / ``include_tests``) is intentionally NOT in this list —
# passing ``sections=[…]`` should not count as narrowing. Naming these
# explicitly on the model prevents the caller-discipline bug the old
# ``**kwargs`` helper had (any new output-control arg would silently
# defeat empty-call detection).
_NARROWING_FIELDS: tuple[str, ...] = (
    "query_text",
    "ids",
    "kind",
    "type",
    "entity_type",
    "file_extension",
    "path_prefix",
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
    "needs_refactoring",
    "vulnerabilities",
    "frameworks",
    "domain",
    "concerns",
    "layers",
    "patterns",
    "keywords",
    "file_issues",
)


class QueryInput(BaseModel):
    """Full ``codeindex_query`` parameter bundle.

    Collapses the 34-arg service surface into a single typed input
    object. Owns validation (mutual exclusion of ``query_text`` and
    ``ids``), empty-call detection, and construction of the two filter
    envelopes.

    The agent-facing :meth:`CodeIndexTools.codeindex_query` keeps its
    flat signature — agno derives the LLM tool schema from that method
    directly. The toolkit builds a :class:`QueryInput` on the first
    line and forwards it to :class:`QueryService.run` so all the
    downstream code sees a single typed object instead of 34 loose
    kwargs.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── search / direct fetch ──
    query_text: str | None = None
    ids: list[str] | None = None

    # ── structural scope ──
    kind: Kind | None = None
    type: str | None = None
    entity_type: str | list[str] | None = None
    file_extension: str | None = None
    path_prefix: str | None = None

    # ── quality categoricals ──
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
    needs_refactoring: bool | None = None

    # ── list-shaped categories ──
    vulnerabilities: list[str] | None = None
    frameworks: list[str] | None = None
    domain: list[str] | None = None
    concerns: list[str] | None = None
    layers: list[str] | None = None
    patterns: list[str] | None = None
    keywords: list[str] | None = None
    file_issues: list[str] | None = None

    # ── output control ──
    sections: list[Section] | None = None
    limit: int = 20
    commit: str | None = None
    include_tests: bool = False

    @classmethod
    def from_tool_kwargs(cls, **kwargs: Any) -> QueryInput:
        """Build a :class:`QueryInput` from the toolkit method's kwargs.

        Filters ``kwargs`` through :attr:`model_fields` so callers can
        splat ``**locals()`` (or any superset) without pulling in
        stray frame locals like ``self``. Keeps the toolkit method
        from re-listing every field name — that duplication was the
        audit's biggest offender in ``tool.py``.
        """
        allowed = set(cls.model_fields) & kwargs.keys()
        return cls(**{name: kwargs[name] for name in allowed})

    def validate_scope(self) -> ErrorResponse | None:
        """Return a mutual-exclusion :class:`ErrorResponse` when the
        caller supplied both ``query_text`` and ``ids``. ``None`` when
        the scope is well-formed.
        """
        if self.query_text and self.ids:
            return ErrorResponse(error="pass either query_text or ids, not both")
        return None

    def is_empty_call(self) -> bool:
        """True iff no narrowing input was supplied.

        A call is "empty" when every narrowing field is ``None`` (or an
        empty list for list-shaped categories). Bool ``needs_refactoring``
        is meaningful even when ``False`` — that's the "items that don't
        need refactoring" query, so any non-``None`` bool counts as
        narrowing.
        """
        if self.query_text:
            return False
        if self.ids:
            return False
        for name in _NARROWING_FIELDS:
            if name in ("query_text", "ids"):
                continue
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, list) and not value:
                continue
            return False
        return True

    def empty_call_error(self) -> ErrorResponse:
        """The didactic error surfaced when :meth:`is_empty_call` fires.

        Lives on the model so the didactic prose sits next to the
        check that decides whether to raise it — the error text names
        the exact filters the model knows about.
        """
        return ErrorResponse(
            error=(
                "codeindex_query was called without any narrowing input — "
                "no query_text, no ids, no filters set. The call would return "
                "arbitrary items.\n\n"
                "If you meant to triage by severity / quality, pass a typed filter with "
                "actual values, not `None`. Examples:\n"
                "  codeindex_query(security=['major-issues','critical'])\n"
                "  codeindex_query(vulnerabilities=['hardcoded-secret','sql-injection'])\n"
                "  codeindex_query(needs_refactoring=True, priority=['high','critical'])\n\n"
                "Note: passing `security=None` (or any other typed-filter arg as None) "
                "is the SAME as not passing it — None means 'no filter on this dimension'. "
                "Pass a list of severity values instead."
            )
        )

    def categorical_filters(self) -> _CategoricalFilters:
        """Build the :class:`_CategoricalFilters` envelope (chroma-side)."""
        return _CategoricalFilters.from_flat_args(self)

    def list_filters(self) -> _ListFilters:
        """Build the :class:`_ListFilters` envelope (Python-side)."""
        return _ListFilters.from_flat_args(self)

    def telemetry_dict(self) -> dict[str, Any]:
        """Return a compact args dict for eval telemetry.

        Drops ``None`` and empty-list values so the log entry stays
        readable; ``include_tests`` is only surfaced when ``True`` (the
        default is boring — logging every ``False`` clutters diffs).
        """
        raw = self.model_dump(exclude_none=True)
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if v == [] or v == "":
                continue
            if k == "include_tests" and not v:
                continue
            if k == "limit" and v == 20:
                # Default limit — no reason to log it.
                continue
            out[k] = v
        return out


class RenderedRow(BaseModel):
    """A :class:`CodeIndexResult` paired with its raw and filtered content.

    The tree builder needs BOTH shapes: matched leaves get the section-
    filtered content (only what the caller asked for), while intermediate
    ancestor nodes fall back to the unfiltered raw content so their
    ``summary`` field survives when the caller requested a non-summary
    section like ``security``.

    Previously the query service smuggled the raw content back onto the
    ``CodeIndexResult`` via a ``_raw_content`` sidecar attribute; this
    wrapper replaces that pattern with a proper typed pair. Ancestors
    fetched during tree walk get ``raw_content == filtered_content``
    (they aren't filtered at fetch time).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    row: CodeIndexResult
    raw_content: str = ""
    filtered_content: str = ""

    @classmethod
    def wrap_ancestor(cls, row: CodeIndexResult) -> RenderedRow:
        """Wrap an ancestor row where the raw and filtered content are the same.

        Ancestors are fetched by id during tree walk and never section-
        filtered, so both fields collapse to ``row.content``.
        """
        return cls(row=row, raw_content=row.content, filtered_content=row.content)


# ── Tree input ───────────────────────────────────────────────────────


class TreeInput(BaseModel):
    """``codeindex_tree`` parameter bundle.

    Mirrors :class:`QueryInput`'s shape for the drill-down tool so
    the toolkit method can hand a single typed object to
    :meth:`TreeService.run` and to the telemetry recorder — without
    re-listing every field name.

    The public ``id`` kwarg name is preserved on the model (agno
    derives the LLM tool schema from the toolkit method's signature,
    which still uses ``id``). :meth:`for_service` renames ``id`` to
    ``item_id`` internally so ``TreeService.run`` keeps its existing
    signature.
    """

    id: str
    sections: list[Section] | None = None
    relations: list[Relation] | None = None
    commit: str | None = None

    @classmethod
    def from_tool_kwargs(cls, **kwargs: Any) -> TreeInput:
        """Build a :class:`TreeInput` from the toolkit method's kwargs.

        Filters ``kwargs`` through :attr:`model_fields` so ``**locals()``
        stays safe (stray frame locals like ``self`` are ignored).
        """
        allowed = set(cls.model_fields) & kwargs.keys()
        return cls(**{name: kwargs[name] for name in allowed})

    def for_service(self) -> dict[str, Any]:
        """Return the kwargs :meth:`TreeService.run` accepts.

        Renames the public ``id`` to the service's ``item_id`` so the
        tool method doesn't have to translate. Values are passed
        through unchanged (including ``None`` sections / relations /
        commit — the service treats those as "use defaults").
        """
        return {
            "item_id": self.id,
            "sections": self.sections,
            "relations": self.relations,
            "commit": self.commit,
        }

    def telemetry_dict(self) -> dict[str, Any]:
        """Return a compact args dict for eval telemetry.

        Drops ``None`` and empty-list values so the log entry stays
        readable. Matches :meth:`QueryInput.telemetry_dict` shape.
        """
        raw = self.model_dump(exclude_none=True)
        return {k: v for k, v in raw.items() if v != [] and v != ""}


# ── Telemetry ────────────────────────────────────────────────────────


class TelemetryEntry(BaseModel):
    """One row in the eval telemetry log.

    Replaces the two raw-dict literals the old toolkit built inline
    in ``codeindex_query`` and ``codeindex_tree``. Serialized as one
    JSON line per invocation via :meth:`TelemetryLog.record`.
    """

    ts: float
    tool: str
    duration_ms: float
    args: dict[str, Any]
    response: str
    response_chars: int
