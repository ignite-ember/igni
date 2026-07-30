"""Query path for ``codeindex_query`` — the wide-net search/filter tool.

Owns the orchestration of:

  - argument validation (mutual exclusion, empty-call guard)
  - filter envelope construction (categorical + list)
  - calling :meth:`CodeIndex.search` or :meth:`CodeIndex.filter_items`
  - post-filtering by list categories and test-path exclusion
  - section-trimming each result's content
  - delegating disambiguation refs to :class:`DisambiguationService`
  - delegating tree assembly to :class:`TreeBuilder`

The toolkit ``codeindex_query`` method is a thin façade over
:meth:`QueryService.run` plus telemetry; all the actual logic lives
here. The service returns typed ``ItemsResponse | ErrorResponse``
unions — the toolkit serializes them at the agent boundary via
:class:`JsonSerializer`.
"""

from __future__ import annotations

import logging

from ember_code.core.code_index.enums import Section
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.schema.items import CodeIndexResult
from ember_code.core.code_index.schema.where_filter import ChromaWhereFilter
from ember_code.core.tools.codeindex.disambiguation import DisambiguationService
from ember_code.core.tools.codeindex.schemas import (
    ErrorResponse,
    ItemsResponse,
    QueryInput,
    RenderedRow,
    _DisambiguationGroup,
)
from ember_code.core.tools.codeindex.section_markup import SectionMarkup
from ember_code.core.tools.codeindex.test_paths import TestPathClassifier
from ember_code.core.tools.codeindex.tree_builder import TreeBuilder

logger = logging.getLogger(__name__)


class QueryService:
    """Owns the ``codeindex_query`` execution path.

    Construct once with the shared :class:`CodeIndex`; call
    :meth:`run` per query. Returns a typed ``ItemsResponse |
    ErrorResponse`` — the toolkit serializes at the boundary so this
    module stays out of the JSON-formatting business.
    """

    def __init__(self, idx: CodeIndex):
        self._idx = idx
        self._disambig = DisambiguationService(idx)

    async def run(self, params: QueryInput) -> ItemsResponse | ErrorResponse:
        """Run one ``codeindex_query`` invocation.

        :class:`QueryInput` owns the precondition checks (mutual
        exclusion, empty-call detection) and the filter-envelope
        splitting — this method just orchestrates the three
        downstream phases: resolve commit, fetch+filter, render.
        """
        # ── precondition checks (owned by the input model) ──
        if err := params.validate_scope():
            return err
        if params.is_empty_call():
            return params.empty_call_error()

        sha_or_err = self._resolve_commit(params.commit)
        if isinstance(sha_or_err, ErrorResponse):
            return sha_or_err
        sha = sha_or_err

        rows, truncated = await self._fetch_and_filter(params=params, sha=sha)
        return await self._render(
            params=params,
            sha=sha,
            rows=rows,
            truncated=truncated,
        )

    # ── phase 1: commit resolution ───────────────────────────────────

    def _resolve_commit(self, commit: str | None) -> str | ErrorResponse:
        """Resolve the target commit or return the caller-visible error.

        Returns the SHA on success; an :class:`ErrorResponse` when the
        index is empty or the caller asked for a commit that wasn't
        indexed.
        """
        sha = commit or self._idx.head()
        if not sha:
            return ErrorResponse(error="no head commit; index may be empty")
        if not self._idx.has_commit(sha):
            return ErrorResponse(error=f"no chroma index for commit {sha}")
        return sha

    # ── phase 2: fetch + post-filter ─────────────────────────────────

    async def _fetch_and_filter(
        self,
        *,
        params: QueryInput,
        sha: str,
    ) -> tuple[list[CodeIndexResult], bool]:
        """Run the chroma call, apply Python-side filters, cap at ``limit``.

        Returns ``(rows, truncated)`` where ``truncated`` reflects
        whether chroma had more candidates upstream — computed from
        the PRE-filter count so it doesn't silently answer the wrong
        question ("did the filters happen to leave exactly ``limit``
        items") when the real question is "were more hits available?".
        """
        where = params.categorical_filters().to_where()
        list_filters = params.list_filters()

        # Over-fetch when post-filtering is in play — chroma can't push
        # ``$contains`` down for list filters, and test-path filtering
        # is path-pattern-based which also has to happen Python-side.
        # Direct-id fetches (``ids=[…]``) skip the test filter — the
        # caller asked for these specific items.
        excluding_tests = (not params.include_tests) and not params.ids
        limit = params.limit
        fetch_limit = limit * 4 if list_filters.has_any or excluding_tests else limit

        rows = await self._fetch_rows(
            query_text=params.query_text,
            ids=params.ids,
            where=where,
            limit=fetch_limit,
            sha=sha,
        )

        # Snapshot the pre-filter count BEFORE any Python-side filters
        # and the ``[:limit]`` slice. ``truncated`` needs to mean
        # "chroma would have returned more if we hadn't capped"; the
        # post-filter length answers a different question.
        pre_filter_count = len(rows)

        if list_filters.has_any:
            rows = [r for r in rows if list_filters.matches(r)]
        if excluding_tests:
            rows = [r for r in rows if not TestPathClassifier.is_test(r.path)]
        rows = rows[:limit]

        truncated = pre_filter_count >= fetch_limit
        return rows, truncated

    async def _fetch_rows(
        self,
        *,
        query_text: str | None,
        ids: list[str] | None,
        where: ChromaWhereFilter | None,
        limit: int,
        sha: str,
    ) -> list[CodeIndexResult]:
        """Delegate to :meth:`CodeIndex.search` or :meth:`CodeIndex.filter_items`."""
        if query_text:
            return await self._idx.search(query=query_text, limit=limit, commit=sha, where=where)
        return await self._idx.filter_items(where=where, ids=ids, limit=limit, commit=sha)

    # ── phase 3: render (refs + tree) ────────────────────────────────

    async def _render(
        self,
        *,
        params: QueryInput,
        sha: str,
        rows: list[CodeIndexResult],
        truncated: bool,
    ) -> ItemsResponse:
        """Attach disambiguation refs, build the tree, return the response envelope."""
        sections: tuple[Section, ...] = (
            tuple(params.sections) if params.sections else Section.default_group()
        )
        refs_map = await self._refs_for(
            rows=rows,
            query_text=params.query_text,
            ids=params.ids,
            sha=sha,
        )
        rendered = self._render_rows(rows=rows, sections=sections)
        tree_items = await TreeBuilder(
            idx=self._idx,
            ranked_rows=rendered,
            sha=sha,
            refs_map=refs_map or {},
        ).build()

        return ItemsResponse(
            commit=sha,
            items=tree_items,
            total=len(rows),
            truncated=truncated,
        )

    async def _refs_for(
        self,
        *,
        rows: list[CodeIndexResult],
        query_text: str | None,
        ids: list[str] | None,
        sha: str,
    ) -> dict[str, _DisambiguationGroup] | None:
        """Fetch the disambiguation refs map or ``None`` when it's not warranted.

        Refs only make sense when the caller ran a text search and got
        multiple hits back (single-hit disambiguation has nothing to
        disambiguate against). Direct-id fetches skip it entirely.
        Failures are logged and swallowed — refs are supplemental
        signal, never load-bearing.
        """
        if not query_text or len(rows) <= 1 or ids:
            return None
        try:
            return await self._disambig.refs_for(
                items=rows[: DisambiguationService.TOP_N],
                query_text=query_text,
                sha=sha,
            )
        except Exception:  # noqa: BLE001 — CodeIndex has no typed exception surface.
            # Follow-up: narrow this once chroma / sqlite exceptions
            # bubble up as a documented class.
            logger.exception("disambiguation refs failed")
            return None

    @staticmethod
    def _render_rows(
        *,
        rows: list[CodeIndexResult],
        sections: tuple[Section, ...],
    ) -> list[RenderedRow]:
        """Section-filter each row's content, returning typed pairs.

        Each :class:`RenderedRow` carries BOTH shapes: the raw content
        (used by the tree builder for intermediate-node summaries so
        the ancestor "what is this folder" framing survives non-summary
        section requests) and the filtered content (what a matched
        leaf actually renders in the response). The dual-content pair
        replaced an earlier pattern that stashed the raw content on
        the row as a ``_raw_content`` sidecar attribute — that hack is
        gone; the invariant is now typed.
        """
        return [
            RenderedRow(
                row=r,
                raw_content=r.content,
                filtered_content=SectionMarkup(r.content).keep(sections),
            )
            for r in rows
        ]
