"""Tree assembly for ``codeindex_query`` responses.

The service returns a **forest**: matched rows are grouped by their
root ancestor, ancestor chains are walked out to the immediate folder,
and each node carries its unmatched-sibling names for context.
:class:`TreeBuilder` owns every step of that assembly.

Pulling this out of :class:`QueryService` means:

  - The tree-walk state (``nodes_by_id`` / ``chains`` / ``siblings_by_parent``
    / ``is_matched`` / ``score_by_id``) lives on an instance instead of
    being threaded through recursion as kwargs.
  - The service reads as three named phases (fetch → build tree →
    render) instead of one 200-line method.
  - The raw-content sidecar attribute the previous ``_build_tree``
    smuggled onto :class:`CodeIndexResult` becomes a proper typed
    :class:`RenderedRow` pair; the builder consumes typed rows, no
    private-attr reach-in.
"""

from __future__ import annotations

import logging

from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.schema.items import CodeIndexResult
from ember_code.core.code_index.schema.where_filter import ChromaWhereFilter
from ember_code.core.tools.codeindex.schemas import (
    RenderedRow,
    _DisambiguationGroup,
    _TreeNode,
)
from ember_code.core.tools.codeindex.section_markup import SectionMarkup

logger = logging.getLogger(__name__)

# Sibling lookup is uncapped by design: names are short, and a folder
# with many files is exactly the case where the agent benefits from
# seeing every peer (otherwise it might miss the right module). Pass a
# generous limit to chroma so it doesn't truncate.
_SIBLINGS_FETCH_LIMIT = 10_000


class TreeBuilder:
    """Assembles the nested-tree response from a flat ranked list.

    Construct one per query — the builder holds per-query state (the
    ranked rows, the ancestor cache, the sibling map) so the recursive
    ``_assemble`` method doesn't have to thread it through kwargs.
    """

    def __init__(
        self,
        *,
        idx: CodeIndex,
        ranked_rows: list[RenderedRow],
        sha: str,
        refs_map: dict[str, _DisambiguationGroup],
    ) -> None:
        self._idx = idx
        self._ranked_rows = ranked_rows
        self._sha = sha
        self._refs_map = refs_map

        # State built by :meth:`build` before the recursive assemble
        # step. Kept as instance attributes so ``_assemble`` doesn't
        # accept them as kwargs on every recursive call.
        self._nodes_by_id: dict[str, RenderedRow] = {r.row.item_id: r for r in ranked_rows}
        self._chains: dict[str, list[str]] = {}
        self._siblings_by_parent: dict[str, list[str]] = {}
        self._is_matched: set[str] = {r.row.item_id for r in ranked_rows}
        self._score_by_id: dict[str, float] = {
            r.row.item_id: r.row.score or 0.0 for r in ranked_rows
        }

    async def build(self) -> list[_TreeNode]:
        """Walk each row's parent chain, fetch siblings, assemble the forest."""
        if not self._ranked_rows:
            return []

        await self._walk_ancestors()
        self._siblings_by_parent = await self._fetch_siblings(
            parent_ids={n.row.parent_id for n in self._nodes_by_id.values() if n.row.parent_id},
        )
        self._build_chains()

        return self._assemble(
            row_ids=[r.row.item_id for r in self._ranked_rows],
            depth=0,
        )

    # ── phase 1: ancestor walk ───────────────────────────────────────

    async def _walk_ancestors(self) -> None:
        """BFS up the ``parent_id`` chain until every chain hits a folder.

        One batched fetch per BFS level — typically depth 3-4 in
        practice. Ancestors get wrapped as :class:`RenderedRow` with
        ``raw_content == filtered_content`` because they weren't
        section-filtered (they're context, not matches).
        """
        to_fetch: set[str] = {
            r.row.parent_id
            for r in self._ranked_rows
            if r.row.parent_id and r.row.parent_id not in self._nodes_by_id
        }
        while to_fetch:
            ancestors = await self._idx.filter_items(
                ids=list(to_fetch), limit=len(to_fetch), commit=self._sha
            )
            next_fetch: set[str] = set()
            for a in ancestors:
                self._nodes_by_id[a.item_id] = RenderedRow.wrap_ancestor(a)
                # Stop walking once we reach a folder — that's the
                # immediate-folder layer. Going further (app/,
                # app/services/) adds noise without much signal.
                if a.type == "folder":
                    continue
                if a.parent_id and a.parent_id not in self._nodes_by_id:
                    next_fetch.add(a.parent_id)
            to_fetch = next_fetch

    # ── phase 2: sibling name fetch ──────────────────────────────────

    async def _fetch_siblings(
        self,
        *,
        parent_ids: set[str],
    ) -> dict[str, list[str]]:
        """For each parent id, return its child names.

        Capped at ``_SIBLINGS_FETCH_LIMIT`` (10k) per parent — far above
        any normal folder / file / class size, but cheap to surface
        when it does happen. If the cap is hit we log a warning so the
        truncation isn't invisible; the agent still gets a useful
        subset, just incomplete.
        """
        result: dict[str, list[str]] = {}
        for pid in parent_ids:
            try:
                children = await self._idx.filter_items(
                    where=ChromaWhereFilter.equal("parent_id", pid),
                    limit=_SIBLINGS_FETCH_LIMIT,
                    commit=self._sha,
                )
            except Exception:  # noqa: BLE001 — CodeIndex has no typed exception surface.
                # Follow-up: narrow this once chroma / sqlite exceptions
                # bubble up as a documented class.
                logger.exception("sibling fetch failed for parent_id=%s", pid)
                continue
            if len(children) >= _SIBLINGS_FETCH_LIMIT:
                logger.warning(
                    "sibling fetch hit cap for parent_id=%s (%d items returned, "
                    "additional children silently dropped). Bump "
                    "_SIBLINGS_FETCH_LIMIT in tree_builder.py if this is real.",
                    pid,
                    len(children),
                )
            result[pid] = [c.name for c in children if c.name]
        return result

    # ── phase 3: chain construction ──────────────────────────────────

    def _build_chains(self) -> None:
        """For each matched row, walk its parent chain up to the immediate folder."""
        for rendered in self._ranked_rows:
            row = rendered.row
            chain = [row.item_id]
            cur: CodeIndexResult = row
            visited = {row.item_id}
            while cur.parent_id and cur.parent_id in self._nodes_by_id:
                if cur.parent_id in visited:
                    break  # cycle guard
                visited.add(cur.parent_id)
                chain.append(cur.parent_id)
                cur = self._nodes_by_id[cur.parent_id].row
                if cur.type == "folder":
                    break  # stop at the immediate folder
            chain.reverse()
            self._chains[row.item_id] = chain

    # ── phase 4: recursive assemble ──────────────────────────────────

    def _assemble(self, *, row_ids: list[str], depth: int) -> list[_TreeNode]:
        """Group ``row_ids`` by their level-``depth`` ancestor, recurse.

        Recursion bottoms out when a row's chain has no more entries
        past ``depth``.
        """
        groups: dict[str, list[str]] = {}
        for rid in row_ids:
            chain = self._chains[rid]
            if depth >= len(chain):
                continue
            groups.setdefault(chain[depth], []).append(rid)

        out: list[_TreeNode] = []
        for parent_id, members in groups.items():
            parent_row = self._nodes_by_id.get(parent_id)
            if parent_row is None:
                continue
            # Children whose chain extends beyond this level recurse.
            deeper = [rid for rid in members if depth + 1 < len(self._chains[rid])]
            children = self._assemble(row_ids=deeper, depth=depth + 1)

            out.append(self._render_node(parent_row, children))

        out.sort(key=lambda n: n.score or 0.0, reverse=True)
        return out

    # ── phase 5: single-node render ──────────────────────────────────

    def _render_node(
        self,
        parent: RenderedRow,
        children: list[_TreeNode],
    ) -> _TreeNode:
        """Turn one :class:`RenderedRow` + its rendered children into a :class:`_TreeNode`.

        Score is the max of this node's own score (when matched) and any
        matched-descendant score. Summary uses the filtered content for
        matched leaves and the shortened raw content for intermediates
        so ancestor "what is this folder" framing survives even when
        the caller requested a non-summary section.
        """
        parent_row = parent.row
        parent_id = parent_row.item_id

        # Score: max of any matched descendant under this node.
        score: float | None = None
        if parent_id in self._is_matched:
            score = self._score_by_id.get(parent_id)
        descendant_scores = [c.score for c in children if c.score is not None]
        if descendant_scores:
            max_desc = max(descendant_scores)
            score = max_desc if score is None else max(score, max_desc)

        # Summary: full (section-filtered) content for matched leaves;
        # short summary derived from the UNFILTERED content for
        # intermediate nodes. Using the unfiltered content is what gives
        # the agent the "what is this folder" framing even when the
        # matched leaves only requested a non-summary section like
        # ``security`` — otherwise the ancestor summary field would
        # come back empty because the SUMMARY marker was already
        # stripped upstream. :class:`RenderedRow` carries both content
        # shapes explicitly so this branch reads what it means instead
        # of reaching for a sidecar attribute.
        raw_content = parent.raw_content or ""
        if parent_id in self._is_matched and not children:
            summary_text = parent.filtered_content or SectionMarkup(raw_content).shorten()
        else:
            summary_text = SectionMarkup(raw_content).shorten()

        # Siblings: names of OTHER children under this node's parent
        # that aren't on this branch. Exclude the node itself.
        sibling_names: list[str] = []
        if parent_row.parent_id:
            peer_names = self._siblings_by_parent.get(parent_row.parent_id, [])
            sibling_names = [n for n in peer_names if n != parent_row.name]

        # Refs only on entity-level leaves.
        node_refs: _DisambiguationGroup | None = None
        if (
            parent_row.type == "entity"
            and parent_id in self._is_matched
            and not children
            and parent_id in self._refs_map
        ):
            node_refs = self._refs_map[parent_id]

        return _TreeNode(
            item_id=parent_row.item_id,
            type=parent_row.type,
            entity_type=parent_row.entity_type,
            name=parent_row.name,
            path=parent_row.path,
            line_from=parent_row.line_from,
            line_to=parent_row.line_to,
            score=score,
            summary=summary_text,
            siblings=sibling_names,
            matches=children,
            refs=node_refs,
        )
