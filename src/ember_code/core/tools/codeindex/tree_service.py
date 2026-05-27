"""Drill-down path for ``codeindex_tree`` — single-item with reference graph.

Owns:

  - fetching the requested item by uuid
  - section-trimming the item's content
  - attaching every edge involving the item (calls / called_by /
    imports / etc.) as ``item.references = {relation: [ReferenceTarget]}``
  - hydrating each reference target's one-line summary in a single
    batched chroma fetch

Distinct from :class:`QueryService` because the call shape is
different (single item id, no filters, full reference graph) and
because the tree path is the only one that mutates per-item
``references``. Keeps the two paths from sharing accidental state.
"""

from __future__ import annotations

import logging

from ember_code.core.code_index.enums import Relation, Section
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.schema.items import CodeIndexResult, ReferenceTarget
from ember_code.core.tools.codeindex.filters import (
    DEFAULT_SECTIONS,
    filter_sections,
    shorten_summary,
)
from ember_code.core.tools.codeindex.schemas import ErrorResponse, ItemsResponse, _TreeNode

logger = logging.getLogger(__name__)


class TreeService:
    """Owns the ``codeindex_tree`` execution path.

    Construct once with the shared :class:`CodeIndex`; call
    :meth:`run` per drill-down. Returns a JSON string.
    """

    def __init__(self, idx: CodeIndex):
        self._idx = idx

    # Inverse map for each ``Relation`` — used when we observe an edge
    # from its to-side. The edge ``(Z, X, CALLS)`` (Z calls X) appears
    # both for Z (from-side: CALLS → Z's calls list) AND for X (to-side).
    # From X's perspective the relation has flipped semantic: X is *called
    # by* Z, so the bucket name must be the inverse of the stored
    # relation. Without this flip the agent would see Z under X's "calls"
    # — saying "X calls Z" when in fact the opposite is true.
    _INVERSE_RELATION: dict[str, str] = {
        str(Relation.CALLS): str(Relation.CALLED_BY),
        str(Relation.CALLED_BY): str(Relation.CALLS),
        str(Relation.IMPORTS): str(Relation.IMPORTED_BY),
        str(Relation.IMPORTED_BY): str(Relation.IMPORTS),
        str(Relation.EXTENDS): str(Relation.EXTENDED_BY),
        str(Relation.EXTENDED_BY): str(Relation.EXTENDS),
        str(Relation.IMPLEMENTS): str(Relation.IMPLEMENTED_BY),
        str(Relation.IMPLEMENTED_BY): str(Relation.IMPLEMENTS),
        str(Relation.DECORATES): str(Relation.DECORATED_BY),
        str(Relation.DECORATED_BY): str(Relation.DECORATES),
        str(Relation.TYPES_AS): str(Relation.TYPED_BY),
        str(Relation.TYPED_BY): str(Relation.TYPES_AS),
    }

    async def run(
        self,
        *,
        item_id: str,
        sections: list[Section] | None,
        relations: list[Relation] | None,
        commit: str | None,
        json_dumps,
    ) -> str:
        sha = commit or self._idx.head()
        if not sha:
            return json_dumps(ErrorResponse(error="no head commit; index may be empty"))
        if not self._idx.has_commit(sha):
            return json_dumps(ErrorResponse(error=f"no chroma index for commit {sha}"))

        rows = await self._idx.filter_items(ids=[item_id], limit=1, commit=sha)
        if not rows:
            return json_dumps(ErrorResponse(error=f"no item with id {item_id!r}"))

        item = rows[0]
        section_tuple = tuple(sections) if sections else DEFAULT_SECTIONS
        item.content = filter_sections(item.content, section_tuple)

        await self._attach_references(item, relations=relations)

        # The tree path returns a single ``_TreeNode`` with no nested
        # ``matches`` — it's a single-item drill-down, not a search.
        # The full reference graph rides on ``references`` (distinct
        # from ``refs`` which is the query-ranked disambiguation subset).
        node = _TreeNode(
            item_id=item.item_id,
            type=item.type,
            entity_type=item.entity_type,
            name=item.name,
            path=item.path,
            line_from=item.line_from,
            line_to=item.line_to,
            score=item.score,
            summary=item.content,
            references=(
                {rel: [t.model_dump() for t in targets] for rel, targets in item.references.items()}
                if item.references
                else None
            ),
        )
        return ItemsResponse(
            commit=sha,
            items=[node],
            total=1,
            truncated=False,
        ).model_dump_json(indent=2, exclude_none=True)

    # ── private ──────────────────────────────────────────────────────

    async def _attach_references(
        self,
        item: CodeIndexResult,
        *,
        relations: list[Relation] | None,
    ) -> None:
        """Fetch every edge involving ``item.item_id`` from sqlite and
        attach them as ``item.references = {relation: [ReferenceTarget]}``.

        Each target's one-line summary is hydrated in a single batched
        chroma fetch. Items with no edges leave ``item.references`` at
        ``None`` so ``exclude_none=True`` strips the field.
        """
        if not item.item_id:
            return

        relations_str = [str(r) for r in relations] if relations else None
        try:
            edges = await self._idx._file_reference_service().get_by_uuids(
                uuids=[item.item_id], relations=relations_str
            )
        except Exception:
            logger.exception("failed to attach references")
            return

        # Walk each edge once. ``get_by_uuids`` returns edges where the
        # item is on EITHER endpoint, so we see every relation involving
        # ``item`` — typically twice when the indexer's symmetric mirror
        # is intact: once as ``(item, X, CALLS)`` and once as ``(X, item,
        # CALLED_BY)``. Both observations need to bucket into the SAME
        # bucket on ``item`` (here, item's ``called_by``... wait no:
        # ``calls`` for the first, ``called_by`` for the second — both
        # point at X, that's the duplicate to dedupe).
        #
        # When the item is the to-side of an edge, the stored relation is
        # FROM THE OTHER SIDE'S PERSPECTIVE — its semantic is inverted
        # for our item. ``(Z, X, CALLS)`` means Z calls X; from X's POV
        # the relation is CALLED_BY (X is called by Z). The
        # ``_INVERSE_RELATION`` map flips this so the bucket name carries
        # the correct directional meaning. Without the flip the agent
        # would see Z listed under X's ``calls`` — exactly inverting
        # the call graph.
        per_relation: dict[str, list[ReferenceTarget]] = {}
        for e in edges:
            relation = str(e.relation)
            if e.from_uuid == item.item_id:
                target = ReferenceTarget(
                    id=e.to_uuid,
                    name=str(e.meta.get("to_entity_name", "")),
                    path=str(e.meta.get("to_entity_path", "")),
                )
                bucket = relation
            elif e.to_uuid == item.item_id:
                target = ReferenceTarget(
                    id=e.from_uuid,
                    name=str(e.meta.get("from_entity_name", "")),
                    path=str(e.meta.get("from_entity_path", "")),
                )
                # Flip the relation — we're observing this from the
                # opposite side of how it was stored. Fall through to
                # the raw relation only as a last resort if the inverse
                # is unknown (new Relation enum value without an inverse
                # registered above).
                bucket = self._INVERSE_RELATION.get(relation, relation)
                if bucket == relation:
                    logger.warning(
                        "tree_service: no inverse known for relation %r; "
                        "bucketing as-is may invert direction. Update "
                        "_INVERSE_RELATION in tree_service.py.",
                        relation,
                    )
            else:
                continue
            per_relation.setdefault(bucket, []).append(target)

        if not per_relation:
            return

        # Dedupe within each bucket — the symmetric mirror produces two
        # observations of every edge (once from each side), and both now
        # land in the same correctly-named bucket. Preserve first-seen
        # order so the agent gets a stable ordering.
        for rel_name, targets in per_relation.items():
            seen: set[str] = set()
            deduped: list[ReferenceTarget] = []
            for t in targets:
                if t.id not in seen:
                    seen.add(t.id)
                    deduped.append(t)
            per_relation[rel_name] = deduped

        item.references = per_relation

        await self._hydrate_target_summaries(item)

    async def _hydrate_target_summaries(self, item: CodeIndexResult) -> None:
        """Batch-fetch each reference target's SUMMARY-section line and
        attach it to ``ReferenceTarget.summary``. One chroma call for
        every target across every relation. Targets whose source items
        aren't in chroma (or have no summary) keep the ``""`` default.
        """
        if not item.references:
            return
        unique_ids = {
            t.id for targets in item.references.values() for t in targets if t.id and not t.summary
        }
        if not unique_ids:
            return

        try:
            sha = self._idx.head()
            if not sha:
                return
            target_items = await self._idx.filter_items(
                ids=list(unique_ids), limit=len(unique_ids), commit=sha
            )
        except Exception:
            logger.exception("failed to fetch target items for reference summaries")
            return

        id_to_summary = {
            it.item_id: short for it in target_items if (short := shorten_summary(it.content))
        }

        for targets in item.references.values():
            for t in targets:
                s = id_to_summary.get(t.id)
                if s:
                    t.summary = s
