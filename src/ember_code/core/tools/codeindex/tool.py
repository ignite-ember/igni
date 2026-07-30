"""CodeIndexTools — agent-facing toolkit.

Thin facade over :class:`QueryService` and :class:`TreeService`. The
toolkit's only responsibilities are:

  - register the two agent-facing methods (``codeindex_query``,
    ``codeindex_tree``) with the agno toolkit machinery,
  - build a typed input bundle from each method's flat signature,
  - hand the bundle to :class:`ToolInvocationRecorder`, which owns
    timing + serialization + telemetry + error-wrap.

All retrieval logic, schema construction, and section filtering live
in sibling modules (``services.py``, ``telemetry.py``, ``invocation.py``).
Adding a new feature → new service module, not a new method here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agno.tools import Toolkit

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
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.tools.codeindex.invocation import ToolInvocationRecorder
from ember_code.core.tools.codeindex.schemas import QueryInput, TreeInput
from ember_code.core.tools.codeindex.serializer import JsonSerializer
from ember_code.core.tools.codeindex.services import CodeIndexServices
from ember_code.core.tools.codeindex.telemetry import TelemetryLog

logger = logging.getLogger(__name__)


class CodeIndexTools(Toolkit):
    """Single-tool structured query surface over the per-commit code index.

    Args:
        project_dir: project root used to derive the on-disk path.
            Defaults to ``cwd``.
        data_dir: ember root, defaults to ``~/.ember``.
        index: pre-built :class:`CodeIndex` (used by tests / advanced
            callers). When provided, ``project_dir`` and ``data_dir``
            are ignored.
    """

    def __init__(
        self,
        *,
        project_dir: str | Path | None = None,
        data_dir: str | Path = "~/.ember",
        index: CodeIndex | None = None,
        **kwargs: Any,
    ):
        super().__init__(name="codeindex", **kwargs)
        # Composition: three small classes replace the seven-concern
        # blob the toolkit used to be. The services own the CodeIndex
        # lifecycle, the telemetry log owns the file-append sink, the
        # recorder owns the timing → serialize → record → error-wrap
        # scaffolding that used to duplicate across both tool methods.
        self._services = CodeIndexServices(
            project_dir=Path(str(project_dir)) if project_dir else Path.cwd(),
            data_dir=data_dir,
            explicit_index=index,
        )
        self._serializer = JsonSerializer()
        self._recorder = ToolInvocationRecorder(
            serializer=self._serializer,
            telemetry=TelemetryLog(),
        )
        self.register(self.codeindex_query)
        self.register(self.codeindex_tree)

    @property
    def _explicit_index(self) -> CodeIndex:
        """Backward-compat handle to the underlying :class:`CodeIndex`.

        Tests monkeypatch ``search`` on this attribute
        (``tests/test_codeindex_tools.py::test_internal_exception_surfaces_error``);
        the property forwards to :attr:`CodeIndexServices.index` so the
        returned object IS the same handle the services close over,
        not a copy.
        """
        return self._services.index

    async def close(self) -> None:
        """Close the underlying :class:`CodeIndex`.

        Matches the historical semantics: whichever ``CodeIndex`` the
        services hold (whether injected or self-built) is closed.
        """
        await self._services.close()

    # ── codeindex_query — search/filter ───────────────────────────────

    async def codeindex_query(
        self,
        # ── what you're searching ──
        query_text: str | None = None,
        # ── direct fetch ──
        ids: list[str] | None = None,
        # ── structural scope ──
        kind: Kind | None = None,
        type: str | None = None,
        entity_type: str | list[str] | None = None,
        file_extension: str | None = None,
        path_prefix: str | None = None,
        # ── quality categoricals (single value or list = OR) ──
        quality: QualityLevel | list[QualityLevel] | None = None,
        complexity: ComplexityLevel | list[ComplexityLevel] | None = None,
        security: SecurityLevel | list[SecurityLevel] | None = None,
        testing: TestingLevel | list[TestingLevel] | None = None,
        testability: TestabilityLevel | list[TestabilityLevel] | None = None,
        documentation: DocumentationLevel | list[DocumentationLevel] | None = None,
        performance: PerformanceLevel | list[PerformanceLevel] | None = None,
        issues: IssuesSeverity | list[IssuesSeverity] | None = None,
        maintainability: QualityLevel | list[QualityLevel] | None = None,
        architecture: QualityLevel | list[QualityLevel] | None = None,
        technical_debt: TechnicalDebtLevel | list[TechnicalDebtLevel] | None = None,
        cohesion: CohesionLevel | list[CohesionLevel] | None = None,
        coupling: CouplingLevel | list[CouplingLevel] | None = None,
        stability: StabilityLevel | list[StabilityLevel] | None = None,
        priority: PriorityLevel | list[PriorityLevel] | None = None,
        needs_refactoring: bool | None = None,
        # ── list-shaped categories (each a list — OR within) ──
        vulnerabilities: list[str] | None = None,
        frameworks: list[str] | None = None,
        domain: list[str] | None = None,
        concerns: list[str] | None = None,
        layers: list[str] | None = None,
        patterns: list[str] | None = None,
        keywords: list[str] | None = None,
        file_issues: list[str] | None = None,
        # ── output control ──
        sections: list[Section] | None = None,
        limit: int = 20,
        commit: str | None = None,
        # ── test-files filter ──
        include_tests: bool = False,
    ) -> str:
        """Search / filter the code index — returns a list of items.

        This tool **never returns reference data** for the items
        themselves — to explore a specific item's edges (calls,
        called_by, imports, …), use ``codeindex_tree`` after picking
        a uuid here. However, when ``query_text`` is used and 2+
        items come back, the response includes a top-level ``refs``
        map: for the top-5 items, the most-relevant callers and
        callees ranked by similarity to the same ``query_text``. Use
        that map to disambiguate near-miss candidates whose summaries
        look superficially similar.

        **Test files are excluded by default.** Most agent queries
        are looking for production-shape code to extend or imitate;
        test files are noise. Pass ``include_tests=True`` if you
        actually need to search test code (e.g. "find an existing
        test fixture", "audit a flaky test"). The exclusion uses
        path conventions: items under ``tests/`` / ``test/`` /
        ``__tests__/``, or with ``test_*.py`` / ``*_test.{py,go}`` /
        ``*.{test,spec}.{js,ts,jsx,tsx,mjs}`` filenames. Direct-id
        fetches (``ids=[…]``) are not affected — if the caller
        asked for a specific test item by uuid, they get it.

        Args:
            query_text: natural-language search ("auth flow", "memory leak").
                When set, runs semantic search; otherwise runs filter-only fetch.
            ids: fetch specific item ids directly. Mutually exclusive with
                ``query_text``.
            kind: ``"code"`` or ``"docs"``.
            type: ``"file"``, ``"folder"``, or ``"entity"``.
            entity_type: ``"function"``, ``"class"``, ``"section"``, etc.
                Pass a list for OR.
            file_extension: ``".py"``, ``".ts"``, etc.
            path_prefix: path scope filter (matches via ``$contains`` for now —
                future versions may switch to a true prefix once chroma supports it).
            quality / complexity / security / testing / testability /
            documentation / performance / issues / maintainability /
            architecture / technical_debt / cohesion / coupling / stability /
            priority: each takes one enum value or a list (list = OR).
            needs_refactoring: bool filter.
            vulnerabilities / frameworks / domain / concerns / layers /
            patterns / keywords / file_issues: lists. Multiple values OR
            within one category. Cross-category is AND.
            sections: which content sections to return per item.
                Pass semantic groups from the ``Section`` enum
                (``summary``, ``quality``, ``security``, ``issues``,
                ``testing``, ``architecture``, ``dependencies``,
                ``recommendations``, ``health_score``, ``entities``).
                Each group resolves to the concrete section names for
                that item type. Default is ``[summary]`` (~5× smaller
                responses).
            limit: max results. Default 20.
            commit: commit SHA. Defaults to current head.
            include_tests: when False (default), filter out test files
                from the results. Set to True to include them.

        Returns: JSON list response (``ItemsResponse`` shape — items
            without per-item references; for those use ``codeindex_tree``).
            Top-level ``refs`` carries disambiguating callers/callees
            for the top items.
        """
        # The agent-facing signature stays wide (agno derives the LLM
        # tool schema from THIS method's signature, so it must remain
        # a flat list of typed kwargs); the toolkit only bundles the
        # kwargs into a typed input and hands off from here.
        params = QueryInput.from_tool_kwargs(**locals())
        return await self._recorder.invoke(
            tool_name="codeindex_query",
            telemetry_args=params.telemetry_dict(),
            coro=self._services.query().run(params),
        )

    # ── codeindex_tree — single-item drill-down ───────────────────────

    async def codeindex_tree(
        self,
        id: str,
        sections: list[Section] | None = None,
        relations: list[Relation] | None = None,
        commit: str | None = None,
    ) -> str:
        """Drill into one item — fetch it plus every reference edge.

        Use this *after* ``codeindex_query`` has surfaced an item id
        you want to explore. The response is one ``CodeIndexResult``
        with ``references`` populated as
        ``{relation: [ReferenceTarget, …]}``: every immediate caller,
        callee, importer, etc. with id/name/path/summary, ready for
        the next ``codeindex_query(ids=[…])`` follow-up.

        Args:
            id: the uuid of the item (file / entity / folder) to expand.
            sections: which content sections to keep on the item itself
                (``Section`` enum groups). Default ``[summary]``.
            relations: only return edges with these relation kinds
                (``calls``, ``called_by``, ``imports``, ``imported_by``,
                etc.). Default: all kinds.
            commit: commit SHA. Defaults to current head.

        Returns: JSON ``ItemsResponse`` shape with a single item; the
            item's ``references`` map carries the full edge graph.
        """
        params = TreeInput.from_tool_kwargs(**locals())
        return await self._recorder.invoke(
            tool_name="codeindex_tree",
            telemetry_args=params.telemetry_dict(),
            coro=self._services.tree().run(**params.for_service()),
        )
