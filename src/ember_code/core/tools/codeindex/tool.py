"""CodeIndexTools — agent-facing toolkit.

Thin facade over :class:`QueryService` and :class:`TreeService`. The
toolkit's job is just to:

  - register the two agent-facing methods (``codeindex_query``,
    ``codeindex_tree``) with the agno toolkit machinery,
  - resolve the shared :class:`CodeIndex` lazily,
  - record telemetry for each call,
  - delegate the actual work to a service.

All retrieval logic, schema construction, and section filtering live
in sibling modules. Adding a new feature → new service module, not a
new method here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from agno.tools import Toolkit
from pydantic import BaseModel

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
from ember_code.core.tools.codeindex.query_service import QueryService
from ember_code.core.tools.codeindex.schemas import ErrorResponse
from ember_code.core.tools.codeindex.tree_service import TreeService

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
        self._explicit_index = index
        self._project_dir = Path(str(project_dir)) if project_dir else Path.cwd()
        self._data_dir = data_dir
        # Services are lazy: built when ``_ensure_index`` runs the first
        # time, so opening a Toolkit doesn't open chroma until it's used.
        self._query_service: QueryService | None = None
        self._tree_service: TreeService | None = None
        self.register(self.codeindex_query)
        self.register(self.codeindex_tree)

    async def close(self) -> None:
        if self._explicit_index is not None:
            await self._explicit_index.close()

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
        t0 = time.monotonic()
        telemetry_args = self._build_telemetry_args(
            query_text=query_text,
            ids=ids,
            kind=kind,
            type=type,
            entity_type=entity_type,
            file_extension=file_extension,
            path_prefix=path_prefix,
            quality=quality,
            complexity=complexity,
            security=security,
            testing=testing,
            testability=testability,
            documentation=documentation,
            performance=performance,
            issues=issues,
            maintainability=maintainability,
            architecture=architecture,
            technical_debt=technical_debt,
            cohesion=cohesion,
            coupling=coupling,
            stability=stability,
            priority=priority,
            needs_refactoring=needs_refactoring,
            vulnerabilities=vulnerabilities,
            frameworks=frameworks,
            domain=domain,
            concerns=concerns,
            layers=layers,
            patterns=patterns,
            keywords=keywords,
            file_issues=file_issues,
            sections=sections,
            limit=limit,
            commit=commit,
            include_tests=include_tests if include_tests else None,
        )

        try:
            self._ensure_index()
            assert self._query_service is not None  # narrowed by _ensure_index
            response = await self._query_service.run(
                query_text=query_text,
                ids=ids,
                kind=kind,
                type=type,
                entity_type=entity_type,
                file_extension=file_extension,
                path_prefix=path_prefix,
                quality=quality,
                complexity=complexity,
                security=security,
                testing=testing,
                testability=testability,
                documentation=documentation,
                performance=performance,
                issues=issues,
                maintainability=maintainability,
                architecture=architecture,
                technical_debt=technical_debt,
                cohesion=cohesion,
                coupling=coupling,
                stability=stability,
                priority=priority,
                needs_refactoring=needs_refactoring,
                vulnerabilities=vulnerabilities,
                frameworks=frameworks,
                domain=domain,
                concerns=concerns,
                layers=layers,
                patterns=patterns,
                keywords=keywords,
                file_issues=file_issues,
                sections=sections,
                limit=limit,
                commit=commit,
                include_tests=include_tests,
                json_dumps=self._json,
            )
            self._telemetry_log(
                {
                    "ts": time.time(),
                    "tool": "codeindex_query",
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                    "args": telemetry_args,
                    "response": response,
                    "response_chars": len(response),
                }
            )
            return response
        except Exception as exc:
            logger.exception("codeindex_query failed")
            return self._json(ErrorResponse(error=f"codeindex_query failed: {exc}"))

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
        t0 = time.monotonic()
        telemetry_args = {
            k: v
            for k, v in {
                "id": id,
                "sections": sections,
                "relations": relations,
                "commit": commit,
            }.items()
            if v is not None and v != []
        }

        try:
            self._ensure_index()
            assert self._tree_service is not None
            response = await self._tree_service.run(
                item_id=id,
                sections=sections,
                relations=relations,
                commit=commit,
                json_dumps=self._json,
            )
            self._telemetry_log(
                {
                    "ts": time.time(),
                    "tool": "codeindex_tree",
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                    "args": telemetry_args,
                    "response": response,
                    "response_chars": len(response),
                }
            )
            return response
        except Exception as exc:
            logger.exception("codeindex_tree failed")
            return self._json(ErrorResponse(error=f"codeindex_tree failed: {exc}"))

    # ── private ──────────────────────────────────────────────────────

    def _ensure_index(self) -> CodeIndex:
        """Open the ``CodeIndex`` on first use, then build services.

        Lazy so tests can construct the toolkit without touching disk.
        """
        if self._explicit_index is None:
            self._explicit_index = CodeIndex(project=self._project_dir, data_dir=self._data_dir)
        if self._query_service is None:
            self._query_service = QueryService(self._explicit_index)
        if self._tree_service is None:
            self._tree_service = TreeService(self._explicit_index)
        return self._explicit_index

    @staticmethod
    def _json(data: Any) -> str:
        if isinstance(data, BaseModel):
            return data.model_dump_json(indent=2)
        return json.dumps(data, indent=2, default=str)

    @staticmethod
    def _telemetry_log(record: dict[str, Any]) -> None:
        """Append a query/response record to the eval telemetry log if enabled.

        Activated via the ``EMBER_EVAL_TELEMETRY_PATH`` env var. Used by
        the eval runner so reports can show what chroma actually
        returned and what the agent fed back into the conversation.
        No-op when the var is unset.
        """
        path = os.environ.get("EMBER_EVAL_TELEMETRY_PATH")
        if not path:
            return
        try:
            with open(path, "a") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            # Best-effort: never break a real call because the log
            # file is unavailable.
            pass

    @staticmethod
    def _build_telemetry_args(**kwargs: Any) -> dict[str, Any]:
        """Drop ``None`` and empty-list values so the log stays compact."""
        return {k: v for k, v in kwargs.items() if v is not None and v != []}
