"""Reference-graph re-ranking for ``codeindex_query`` responses.

When two candidates have summaries that look similar to the agent
(both mention "Redis sorted sets" / both are in the same folder /
both inherit from the same base), the agent often picks the wrong
one. The summaries can't disambiguate because the discriminating
signal isn't in the text — it's in the **reference graph**: who
calls each entity, what each entity calls.

This service runs after the main chroma search returns:

  1. For each top-N item, fetch every reference edge from sqlite —
     calls, imports, extends, implements, decorates, types_as, and
     each of their inverses. All twelve relation kinds carry signal:
     who calls me, who imports me, who subclasses me, who decorates
     me, what types reference me. We bucket them into outgoing
     ("uses these") and incoming ("is used by these") regardless of
     edge kind — for disambiguation, the connectedness matters more
     than the relation label.
  2. Re-score each edge target's summary against the SAME ``query_text``
     using :meth:`CodeIndex.search_among` — same chroma similarity
     machinery, just restricted to the candidate set.
  3. Keep the top-K per direction.

The agent then sees, alongside the candidates, *what each candidate
is actually used for* — ranked by how relevant each user is to the
intent the agent expressed. Disambiguation becomes mechanical.
"""

from __future__ import annotations

import logging

from ember_code.core.code_index.enums import Relation
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.schema.items import CodeIndexResult
from ember_code.core.tools.codeindex.filters import (
    DISAMBIGUATION_REFS_PER_DIRECTION,
)
from ember_code.core.tools.codeindex.schemas import (
    _DisambiguationGroup,
    _DisambiguationRef,
)

logger = logging.getLogger(__name__)


class DisambiguationService:
    """Builds the per-item refs map surfaced in :class:`ItemsResponse.refs`.

    Stateless aside from the ``CodeIndex`` reference. One instance per
    toolkit lifetime is fine (no per-query state).
    """

    def __init__(self, idx: CodeIndex):
        self._idx = idx

    async def refs_for(
        self,
        *,
        items: list[CodeIndexResult],
        query_text: str,
        sha: str,
    ) -> dict[str, _DisambiguationGroup] | None:
        """Return ``{item_id: DisambiguationGroup}`` for the input items.

        Returns ``None`` when no edges produced any refs — the toolkit
        then omits the field entirely (``exclude_none=True``).

        For items whose direct edge graph is empty (constants, methods
        only called by tests, indirectly-dispatched methods), this
        method falls through to the item's parent (its enclosing
        class or file) and uses the parent's edges as a proxy. Those
        results carry ``via_parent`` so the agent knows the
        relationship is one level up.
        """
        if not items:
            return None
        item_ids = [it.item_id for it in items if it.item_id]
        if not item_ids:
            return None

        # ── Pass 1: direct edges ────────────────────────────────────
        per_item, target_meta = await self._collect_edges(item_ids)

        result: dict[str, _DisambiguationGroup] = {}
        if per_item:
            for iid, dirs in per_item.items():
                group = await self._build_group(
                    dirs=dirs,
                    target_meta=target_meta,
                    query_text=query_text,
                    sha=sha,
                )
                if group.called_by or group.calls:
                    result[iid] = group

        # ── Pass 2: parent fallback ─────────────────────────────────
        #
        # For any item that didn't get refs directly — usually a
        # constant, a method that's only called by tests, or one that's
        # invoked via indirect dispatch — try the item's parent (the
        # enclosing class or file). Class-level edges typically carry
        # the import / instantiation graph that method-level edges miss.
        #
        # We tag the fallback group with ``via_parent`` so the agent
        # reads "AIKeyPool's callers" instead of inferring the method
        # itself is called by these entities.
        parent_lookups: dict[str, str] = {}  # item_id -> parent_id
        for it in items:
            if not it.item_id or not it.parent_id:
                continue
            if it.item_id in result:
                continue  # already has direct edges
            parent_lookups[it.item_id] = it.parent_id

        if parent_lookups:
            parent_ids = list({pid for pid in parent_lookups.values()})
            p_per_item, p_target_meta = await self._collect_edges(parent_ids)
            if p_per_item:
                # Look up parent names + paths for the via_parent label.
                parent_info = await self._fetch_parent_info(parent_ids, sha)
                for child_id, parent_id in parent_lookups.items():
                    dirs = p_per_item.get(parent_id)
                    if not dirs:
                        continue
                    group = await self._build_group(
                        dirs=dirs,
                        target_meta=p_target_meta,
                        query_text=query_text,
                        sha=sha,
                    )
                    if group.called_by or group.calls:
                        info = parent_info.get(parent_id)
                        # Fall back to the full parent_id (not a prefix)
                        # so the agent can still call codeindex_tree(id=…)
                        # on it. The "info is None" branch is the cold
                        # path (metadata fetch returned nothing); when we
                        # do hit it, the LLM consumer would prefer a
                        # usable UUID over a truncated-but-tidy stub.
                        group.via_parent = f"{info.name} ({info.path})" if info else parent_id
                        result[child_id] = group

        return result or None

    async def _build_group(
        self,
        *,
        dirs: dict[str, list[str]],
        target_meta: dict[str, dict[str, str]],
        query_text: str,
        sha: str,
    ) -> _DisambiguationGroup:
        """Rank both directions, dedup self-loops, return a group.

        Shared between the direct-edge path and the parent-fallback
        path so both behave identically (same ranking, same dedup).
        """
        group = _DisambiguationGroup()
        for direction in ("called_by", "calls"):
            refs_list = await self._rank_direction(
                target_ids=dirs[direction],
                query_text=query_text,
                sha=sha,
                target_meta=target_meta,
            )
            if direction == "called_by":
                group.called_by = refs_list
            else:
                group.calls = refs_list

        # Self-loop dedup: when the indexer stores both CALLS and the
        # symmetric CALLED_BY row for a single edge (which it does for
        # most call relationships), the same target lands in both
        # directions with identical scores. Strip duplicates from
        # ``calls`` — ``called_by`` carries the more discriminating
        # signal for disambiguation ("who uses me" tells you the
        # entity's role), so it's the side we keep.
        cb_ids = {r.item_id for r in group.called_by}
        if cb_ids:
            group.calls = [r for r in group.calls if r.item_id not in cb_ids]
        return group

    async def _fetch_parent_info(
        self, parent_ids: list[str], sha: str
    ) -> dict[str, CodeIndexResult]:
        """Hydrate ``{parent_id: CodeIndexResult}`` for the via_parent label."""
        try:
            parents = await self._idx.filter_items(
                ids=parent_ids, limit=len(parent_ids), commit=sha
            )
        except Exception:
            logger.exception("disambiguation: parent hydrate failed")
            return {}
        return {p.item_id: p for p in parents if p.item_id}

    # ── private ──────────────────────────────────────────────────────

    # Every reference relation contributes disambiguation signal — who
    # calls me, who imports me, who extends me, who I decorate, what
    # types reference me. We collapse all 12 relations into two
    # semantic buckets:
    #
    #   outgoing — "this entity uses / depends on those":
    #       CALLS, IMPORTS, EXTENDS, IMPLEMENTS, DECORATES, TYPES_AS
    #   incoming — "this entity is used / depended on by those":
    #       CALLED_BY, IMPORTED_BY, EXTENDED_BY, IMPLEMENTED_BY,
    #       DECORATED_BY, TYPED_BY
    #
    # Mixing all relation kinds in one bucket is intentional: from the
    # agent's disambiguation perspective the only thing that matters is
    # *which other entities are connected and how relevant they are to
    # the query* — the relation kind is secondary. The candidate-set
    # rerank against ``query_text`` already keeps the most informative
    # neighbors regardless of edge kind.
    _OUTGOING_RELATIONS: frozenset[str] = frozenset(
        {
            str(Relation.CALLS),
            str(Relation.IMPORTS),
            str(Relation.EXTENDS),
            str(Relation.IMPLEMENTS),
            str(Relation.DECORATES),
            str(Relation.TYPES_AS),
        }
    )
    _INCOMING_RELATIONS: frozenset[str] = frozenset(
        {
            str(Relation.CALLED_BY),
            str(Relation.IMPORTED_BY),
            str(Relation.EXTENDED_BY),
            str(Relation.IMPLEMENTED_BY),
            str(Relation.DECORATED_BY),
            str(Relation.TYPED_BY),
        }
    )

    async def _collect_edges(
        self, item_ids: list[str]
    ) -> tuple[dict[str, dict[str, list[str]]] | None, dict[str, dict[str, str]]]:
        """One batched sqlite call → bucketed (item, direction) → target_ids.

        Returns ``(None, {})`` on fetch failure or no edges. The second
        return is a name/path cache keyed by target uuid, used later to
        hydrate refs whose chroma row no longer exists.
        """
        try:
            edges = await self._idx._file_reference_service().get_by_uuids(
                uuids=item_ids,
                relations=list(self._OUTGOING_RELATIONS | self._INCOMING_RELATIONS),
            )
        except Exception:
            logger.exception("disambiguation: edge fetch failed")
            return None, {}
        if not edges:
            return None, {}

        # Bucket each edge by source item + direction. ``calls``
        # collects every outgoing relation (calls / imports / extends /
        # implements / decorates / types_as — all "this entity depends
        # on X" semantics); ``called_by`` collects every incoming
        # relation (the reverse direction). The indexer typically
        # stores symmetric pairs (CALLS + CALLED_BY for one call edge,
        # IMPORTS + IMPORTED_BY for one import, etc.); the self-loop
        # dedup in ``_build_group`` collapses those duplicates.
        per_item: dict[str, dict[str, list[str]]] = {
            iid: {"called_by": [], "calls": []} for iid in item_ids
        }
        target_meta: dict[str, dict[str, str]] = {}

        for e in edges:
            relation = str(e.relation)
            from_uuid = e.from_uuid
            to_uuid = e.to_uuid
            from_meta = {
                "name": str(e.meta.get("from_entity_name", "")),
                "path": str(e.meta.get("from_entity_path", "")),
            }
            to_meta = {
                "name": str(e.meta.get("to_entity_name", "")),
                "path": str(e.meta.get("to_entity_path", "")),
            }

            if from_uuid == to_uuid:
                # Self-loops aren't useful for disambiguation.
                continue

            # Edge-direction convention (mirrored by the indexer):
            #   CALLS  edge stored as ``(from=caller, to=callee)``
            #   CALLED_BY edge stored as ``(from=callee, to=caller)``
            # So for any OUTGOING relation, ``from_uuid`` is the source
            # of the dependency and ``to_uuid`` is the target; the
            # source's ``calls`` list should gain the target. For any
            # INCOMING relation it's the opposite: ``from_uuid`` is the
            # depended-on entity, ``to_uuid`` is the caller, and the
            # depended-on entity's ``called_by`` list should gain the
            # caller.
            #
            # ``get_by_uuids`` returns edges where EITHER endpoint is
            # in our batch (see file_reference.py:get_by_uuids), so the
            # same edge can be observed from either side. We handle
            # both — typically the indexer's mirrored pair means we
            # see the same logical relationship twice (once from each
            # side); ``_build_group`` dedupes the resulting duplicates.
            # The historical bug here was that the "to_uuid in per_item"
            # branches wrote into the WRONG bucket (CALLS into called_by
            # and vice versa), silently inverting direction whenever
            # the mirrored pair was incomplete.
            if relation in self._OUTGOING_RELATIONS:
                # ``A --calls--> B``
                if from_uuid in per_item:
                    # A is in batch: A's calls gains B.
                    per_item[from_uuid]["calls"].append(to_uuid)
                    target_meta.setdefault(to_uuid, to_meta)
                if to_uuid in per_item and to_uuid != from_uuid:
                    # B is in batch: B's called_by gains A. (NOT calls
                    # — B doesn't call A; A calls B.)
                    per_item[to_uuid]["called_by"].append(from_uuid)
                    target_meta.setdefault(from_uuid, from_meta)
            elif relation in self._INCOMING_RELATIONS:
                # ``A --called_by--> B``  (semantically: A is called by B)
                if from_uuid in per_item:
                    # A is in batch: A's called_by gains B.
                    per_item[from_uuid]["called_by"].append(to_uuid)
                    target_meta.setdefault(to_uuid, to_meta)
                if to_uuid in per_item and to_uuid != from_uuid:
                    # B is in batch: B's calls gains A. (NOT called_by
                    # — A is called by B, so B is the caller.)
                    per_item[to_uuid]["calls"].append(from_uuid)
                    target_meta.setdefault(from_uuid, from_meta)
            else:
                # Unknown relation — almost always means a new value
                # was added to the ``Relation`` enum without updating
                # the OUTGOING/INCOMING constants here. Log so we
                # notice rather than silently dropping the edge.
                logger.warning(
                    "disambiguation: unknown relation %r (edge "
                    "%s → %s), dropping. Update _OUTGOING_RELATIONS "
                    "/ _INCOMING_RELATIONS in disambiguation.py.",
                    relation,
                    from_uuid,
                    to_uuid,
                )

        # Each direction-list may now contain duplicates because the
        # mirrored pair was observed from both sides (e.g. ``(A,B,CALLS)``
        # AND ``(B,A,CALLED_BY)`` both wrote ``B`` into A's ``calls``
        # via different branches). Dedupe while preserving order so
        # ``_rank_direction`` doesn't double-count downstream.
        for buckets in per_item.values():
            for direction in ("calls", "called_by"):
                seen: set[str] = set()
                deduped: list[str] = []
                for uuid_ in buckets[direction]:
                    if uuid_ not in seen:
                        seen.add(uuid_)
                        deduped.append(uuid_)
                buckets[direction] = deduped

        return per_item, target_meta

    async def _rank_direction(
        self,
        *,
        target_ids: list[str],
        query_text: str,
        sha: str,
        target_meta: dict[str, dict[str, str]],
    ) -> list[_DisambiguationRef]:
        """Re-rank ``target_ids`` by similarity to ``query_text`` and
        keep the top ``DISAMBIGUATION_REFS_PER_DIRECTION``.
        """
        unique_ids = list({t for t in target_ids if t})
        if not unique_ids:
            return []
        try:
            scored = await self._idx.search_among(
                query=query_text,
                candidate_ids=unique_ids,
                limit=DISAMBIGUATION_REFS_PER_DIRECTION,
                commit=sha,
            )
        except Exception:
            logger.exception("disambiguation: search_among failed")
            return []

        refs_list: list[_DisambiguationRef] = []
        for r in scored:
            meta = target_meta.get(r.item_id, {})
            refs_list.append(
                _DisambiguationRef(
                    item_id=r.item_id,
                    name=r.name or meta.get("name", ""),
                    path=r.path or meta.get("path", ""),
                    summary=r.content or "",
                    score=r.score,
                )
            )
        return refs_list
