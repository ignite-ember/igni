"""Query path for ``codeindex_query`` — the wide-net search/filter tool.

Owns the orchestration of:

  - argument validation (mutually-exclusive args, empty-call guard)
  - filter envelope construction (categorical + list)
  - calling :meth:`CodeIndex.search` or :meth:`CodeIndex.filter_items`
  - post-filtering by list categories
  - section-trimming each result's content
  - delegating to :class:`DisambiguationService` for the refs map

The toolkit ``codeindex_query`` method is a thin façade over
:meth:`QueryService.run` plus telemetry; all the actual logic lives
here.
"""

from __future__ import annotations

import logging
from typing import Any

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
    Section,
    SecurityLevel,
    StabilityLevel,
    TechnicalDebtLevel,
    TestabilityLevel,
    TestingLevel,
)
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.schema.items import CodeIndexResult
from ember_code.core.tools.codeindex.disambiguation import DisambiguationService
from ember_code.core.tools.codeindex.empty_guard import is_empty_call
from ember_code.core.tools.codeindex.filters import (
    DEFAULT_SECTIONS,
    DISAMBIGUATION_TOP_N,
    build_where,
    filter_sections,
    is_test_path,
    shorten_summary,
)
from ember_code.core.tools.codeindex.schemas import (
    ErrorResponse,
    ItemsResponse,
    _CategoricalFilters,
    _DisambiguationGroup,
    _ListFilters,
    _TreeNode,
)

# Sibling lookup is uncapped: names are short, and a folder with many
# files is exactly the case where the agent benefits from seeing every
# peer (otherwise it might miss the right module). Pass a generous
# limit to chroma so it doesn't truncate.
_SIBLINGS_FETCH_LIMIT = 10_000

logger = logging.getLogger(__name__)


class QueryService:
    """Owns the ``codeindex_query`` execution path.

    Construct once with the shared :class:`CodeIndex`; call
    :meth:`run` per query. Returns a JSON string (the same shape the
    toolkit method returns to the agent) so the toolkit doesn't have
    to know about Pydantic serialization.
    """

    def __init__(self, idx: CodeIndex):
        self._idx = idx
        self._disambig = DisambiguationService(idx)

    async def run(
        self,
        *,
        query_text: str | None,
        ids: list[str] | None,
        kind: Kind | None,
        type: str | None,
        entity_type: str | list[str] | None,
        file_extension: str | None,
        path_prefix: str | None,
        quality: QualityLevel | list[QualityLevel] | None,
        complexity: ComplexityLevel | list[ComplexityLevel] | None,
        security: SecurityLevel | list[SecurityLevel] | None,
        testing: TestingLevel | list[TestingLevel] | None,
        testability: TestabilityLevel | list[TestabilityLevel] | None,
        documentation: DocumentationLevel | list[DocumentationLevel] | None,
        performance: PerformanceLevel | list[PerformanceLevel] | None,
        issues: IssuesSeverity | list[IssuesSeverity] | None,
        maintainability: QualityLevel | list[QualityLevel] | None,
        architecture: QualityLevel | list[QualityLevel] | None,
        technical_debt: TechnicalDebtLevel | list[TechnicalDebtLevel] | None,
        cohesion: CohesionLevel | list[CohesionLevel] | None,
        coupling: CouplingLevel | list[CouplingLevel] | None,
        stability: StabilityLevel | list[StabilityLevel] | None,
        priority: PriorityLevel | list[PriorityLevel] | None,
        needs_refactoring: bool | None,
        vulnerabilities: list[str] | None,
        frameworks: list[str] | None,
        domain: list[str] | None,
        concerns: list[str] | None,
        layers: list[str] | None,
        patterns: list[str] | None,
        keywords: list[str] | None,
        file_issues: list[str] | None,
        sections: list[Section] | None,
        limit: int,
        commit: str | None,
        include_tests: bool,
        json_dumps: Any,
    ) -> str:
        """Run one ``codeindex_query`` invocation, return the JSON string.

        ``json_dumps`` is injected so the toolkit can apply the same
        formatting (indented, default=str fallback) to error responses
        without coupling this module to the toolkit's helper.
        """
        # ── precondition checks ──
        if query_text and ids:
            return json_dumps(ErrorResponse(error="pass either query_text or ids, not both"))

        if is_empty_call(
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
        ):
            return json_dumps(
                ErrorResponse(
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
            )

        categorical_filters = _CategoricalFilters(
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
        )
        list_filters = _ListFilters(
            vulnerabilities=vulnerabilities or [],
            frameworks=frameworks or [],
            domain=domain or [],
            concerns=concerns or [],
            layers=layers or [],
            patterns=patterns or [],
            keywords=keywords or [],
            file_issues=file_issues or [],
        )

        return await self._search_and_render(
            query_text=query_text,
            where=build_where(categorical_filters),
            list_filters=list_filters,
            ids=ids,
            sections=tuple(sections) if sections else DEFAULT_SECTIONS,
            limit=limit,
            commit=commit,
            include_tests=include_tests,
            json_dumps=json_dumps,
        )

    async def _search_and_render(
        self,
        *,
        query_text: str | None,
        where: dict[str, Any] | None,
        list_filters: _ListFilters,
        ids: list[str] | None,
        sections: tuple[Section, ...],
        limit: int,
        commit: str | None,
        include_tests: bool,
        json_dumps: Any,
    ) -> str:
        """Run the index call, post-filter, attach refs, render JSON."""
        sha = commit or self._idx.head()
        if not sha:
            return json_dumps(ErrorResponse(error="no head commit; index may be empty"))
        if not self._idx.has_commit(sha):
            return json_dumps(ErrorResponse(error=f"no chroma index for commit {sha}"))

        # Over-fetch when post-filtering is in play — chroma can't push
        # ``$contains`` down for list filters, and test-path filtering
        # is path-pattern-based which also has to happen Python-side.
        # Direct-id fetches (``ids=[…]``) skip the test filter — the
        # caller asked for these specific items.
        excluding_tests = (not include_tests) and not ids
        fetch_limit = limit * 4 if list_filters.has_any or excluding_tests else limit

        if query_text:
            rows = await self._idx.search(
                query=query_text, limit=fetch_limit, commit=sha, where=where or None
            )
        else:
            rows = await self._idx.filter_items(
                where=where or None, ids=ids, limit=fetch_limit, commit=sha
            )

        # Snapshot the pre-filter count BEFORE list/test filters and
        # the ``[:limit]`` slice. ``truncated`` needs to mean "chroma
        # would have returned more if we hadn't capped"; computing it
        # post-filter answers a different and useless question
        # ("did the filters happen to leave exactly ``limit`` items"),
        # making the agent think it had exhausted the search space
        # when in fact several hits were quietly filtered out.
        pre_filter_count = len(rows)

        if list_filters.has_any:
            rows = [r for r in rows if list_filters.matches(r)]
        if excluding_tests:
            rows = [r for r in rows if not is_test_path(r.path)]
        rows = rows[:limit]
        # We're truncated iff chroma actually returned a full fetch
        # batch — the post-filter / post-slice length tells us nothing
        # about whether more candidates existed upstream.
        truncated = pre_filter_count >= fetch_limit

        # Disambiguation refs (only when query_text was used and we have
        # multiple rows). Best-effort — never blocks the response.
        refs_map: dict[str, _DisambiguationGroup] | None = None
        if query_text and len(rows) > 1 and not ids:
            try:
                refs_map = await self._disambig.refs_for(
                    items=rows[:DISAMBIGUATION_TOP_N],
                    query_text=query_text,
                    sha=sha,
                )
            except Exception:  # pragma: no cover — defensive
                logger.exception("disambiguation refs failed")
                refs_map = None

        # Stash each row's raw content under a sidecar attribute so the
        # tree builder can fall back to it when generating ``shorten_summary``
        # for intermediate ancestor nodes. Naive section-filtering at this
        # point used to mutate ``r.content`` in place, which then broke
        # ``shorten_summary`` for ancestor nodes whenever the agent
        # requested non-``summary`` sections (e.g. ``sections=['security']``):
        # the SUMMARY section was already stripped, so intermediate nodes
        # came back with empty ``summary`` fields — and the agent lost the
        # "what does this folder do" framing entirely.
        for r in rows:
            r._raw_content = r.content  # type: ignore[attr-defined]
            r.content = filter_sections(r.content, sections)

        tree_items = await self._build_tree(rows, sha, refs_map or {})

        return ItemsResponse(
            commit=sha,
            items=tree_items,
            total=len(rows),
            truncated=truncated,
        ).model_dump_json(indent=2, exclude_none=True)

    # ── Tree assembly ───────────────────────────────────────────────────

    async def _build_tree(
        self,
        ranked_rows: list[CodeIndexResult],
        sha: str,
        refs_map: dict[str, _DisambiguationGroup],
    ) -> list[_TreeNode]:
        """Build the nested-tree response from a flat ranked list.

        Walks each row's ``parent_id`` chain up to the immediate folder,
        groups by root ancestor, attaches sibling names per parent, and
        rides disambiguation refs onto leaf entities.
        """
        if not ranked_rows:
            return []

        # 1. Walk each row's parent chain up to its immediate folder
        #    (or to a node that has no parent). One batched fetch per
        #    BFS level — typically depth 3-4 in practice.
        nodes_by_id: dict[str, CodeIndexResult] = {r.item_id: r for r in ranked_rows}
        to_fetch: set[str] = {
            r.parent_id for r in ranked_rows if r.parent_id and r.parent_id not in nodes_by_id
        }
        while to_fetch:
            ancestors = await self._idx.filter_items(
                ids=list(to_fetch), limit=len(to_fetch), commit=sha
            )
            next_fetch: set[str] = set()
            for a in ancestors:
                nodes_by_id[a.item_id] = a
                # Stop walking once we reach a folder — that's the
                # immediate-folder layer. Going further (app/, app/services/)
                # adds noise without much signal.
                if a.type == "folder":
                    continue
                if a.parent_id and a.parent_id not in nodes_by_id:
                    next_fetch.add(a.parent_id)
            to_fetch = next_fetch

        # 2. For each unique parent that appears in the chains, fetch
        #    its children's *names* for the ``siblings`` list. Batched
        #    by parent_id — one chroma get-call per distinct parent.
        siblings_by_parent = await self._fetch_siblings(
            parent_ids={n.parent_id for n in nodes_by_id.values() if n.parent_id},
            sha=sha,
        )

        # 3. Build chains: row.item_id → [root_id, ..., parent_id, row_id].
        chains: dict[str, list[str]] = {}
        for row in ranked_rows:
            chain = [row.item_id]
            cur = row
            visited = {row.item_id}
            while cur.parent_id and cur.parent_id in nodes_by_id:
                if cur.parent_id in visited:
                    break  # cycle guard
                visited.add(cur.parent_id)
                chain.append(cur.parent_id)
                cur = nodes_by_id[cur.parent_id]
                if cur.type == "folder":
                    break  # stop at the immediate folder
            chain.reverse()
            chains[row.item_id] = chain

        # 4. Recursively assemble: group rows by their root, build
        #    that subtree, recurse on remaining chain.
        is_matched: set[str] = {r.item_id for r in ranked_rows}
        score_by_id: dict[str, float] = {r.item_id: r.score or 0.0 for r in ranked_rows}

        return self._assemble(
            row_ids=[r.item_id for r in ranked_rows],
            chains=chains,
            nodes_by_id=nodes_by_id,
            siblings_by_parent=siblings_by_parent,
            is_matched=is_matched,
            score_by_id=score_by_id,
            refs_map=refs_map,
            depth=0,
        )

    async def _fetch_siblings(
        self,
        *,
        parent_ids: set[str],
        sha: str,
    ) -> dict[str, list[str]]:
        """For each parent id, return its child names.

        Capped at ``_SIBLINGS_FETCH_LIMIT`` (10k) per parent — far above
        any normal folder/file/class size, but cheap to surface when
        it does happen. If the cap is hit we log a warning so the
        truncation isn't invisible; the agent still gets a useful
        subset, just incomplete. Earlier the docstring claimed "no
        cap" but the code did cap — silent + dishonest. Now both
        line up.
        """
        result: dict[str, list[str]] = {}
        for pid in parent_ids:
            try:
                children = await self._idx.filter_items(
                    where={"parent_id": pid},
                    limit=_SIBLINGS_FETCH_LIMIT,
                    commit=sha,
                )
            except Exception:  # pragma: no cover — defensive
                logger.exception("sibling fetch failed for parent_id=%s", pid)
                continue
            if len(children) >= _SIBLINGS_FETCH_LIMIT:
                logger.warning(
                    "sibling fetch hit cap for parent_id=%s (%d items returned, "
                    "additional children silently dropped). Bump "
                    "_SIBLINGS_FETCH_LIMIT in query_service.py if this is real.",
                    pid,
                    len(children),
                )
            result[pid] = [c.name for c in children if c.name]
        return result

    def _assemble(
        self,
        *,
        row_ids: list[str],
        chains: dict[str, list[str]],
        nodes_by_id: dict[str, CodeIndexResult],
        siblings_by_parent: dict[str, list[str]],
        is_matched: set[str],
        score_by_id: dict[str, float],
        refs_map: dict[str, _DisambiguationGroup],
        depth: int,
    ) -> list[_TreeNode]:
        """Group ``row_ids`` by their level-``depth`` ancestor, recurse
        on the next level. The recursion bottoms out when a row's chain
        has no more entries past ``depth``.
        """
        groups: dict[str, list[str]] = {}
        for rid in row_ids:
            chain = chains[rid]
            if depth >= len(chain):
                continue
            groups.setdefault(chain[depth], []).append(rid)

        out: list[_TreeNode] = []
        for parent_id, members in groups.items():
            parent = nodes_by_id.get(parent_id)
            if parent is None:
                continue
            # Children whose chain extends beyond this level recurse.
            deeper = [rid for rid in members if depth + 1 < len(chains[rid])]
            children = self._assemble(
                row_ids=deeper,
                chains=chains,
                nodes_by_id=nodes_by_id,
                siblings_by_parent=siblings_by_parent,
                is_matched=is_matched,
                score_by_id=score_by_id,
                refs_map=refs_map,
                depth=depth + 1,
            )

            # Score: max of any matched descendant under this node.
            score: float | None = None
            if parent_id in is_matched:
                score = score_by_id.get(parent_id)
            descendant_scores = [c.score for c in children if c.score is not None]
            if descendant_scores:
                score = (
                    max(descendant_scores) if score is None else max(score, max(descendant_scores))
                )

            # Summary: full (section-filtered) content for matched leaves;
            # short summary derived from the UNFILTERED content for
            # intermediate nodes. Using the unfiltered content here is
            # what gives the agent the "what is this folder" framing
            # even when the matched leaves only requested a non-summary
            # section like ``security`` — otherwise the ancestor
            # summary field comes back empty because the SUMMARY marker
            # was already stripped.
            raw_content = getattr(parent, "_raw_content", parent.content) or ""
            if parent_id in is_matched and not children:
                summary_text = parent.content or shorten_summary(raw_content)
            else:
                summary_text = shorten_summary(raw_content)

            # Siblings: names of OTHER children under this node's parent
            # that aren't on this branch. Exclude the node itself.
            sibling_names: list[str] = []
            if parent.parent_id:
                peer_names = siblings_by_parent.get(parent.parent_id, [])
                sibling_names = [n for n in peer_names if n != parent.name]

            # Refs only on entity-level leaves.
            node_refs: _DisambiguationGroup | None = None
            if (
                parent.type == "entity"
                and parent_id in is_matched
                and not children
                and parent_id in refs_map
            ):
                node_refs = refs_map[parent_id]

            out.append(
                _TreeNode(
                    item_id=parent.item_id,
                    type=parent.type,
                    entity_type=parent.entity_type,
                    name=parent.name,
                    path=parent.path,
                    line_from=parent.line_from,
                    line_to=parent.line_to,
                    score=score,
                    summary=summary_text,
                    siblings=sibling_names,
                    matches=children,
                    refs=node_refs,
                )
            )

        out.sort(key=lambda n: n.score or 0.0, reverse=True)
        return out
