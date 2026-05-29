"""Tests for ``DisambiguationService.refs_for`` — the public
orchestration method.

This is the path the agent actually traverses when ``codeindex_query``
attaches refs to top-N items. It composes:

  - ``_collect_edges`` (direct edges) — already unit-tested elsewhere.
  - ``_build_group`` (ranking + dedup) — also tested separately.
  - Parent-fallback (this file's main subject): for items with no
    direct edges, walk up to the parent class/file and use ITS edges
    as a proxy, tagged ``via_parent`` so the agent knows the
    relationship is one level up.

The parent-fallback path is what produced the ``parent_id[:8]``
truncation bug. Coverage was zero before today.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.code_index.enums import Relation
from ember_code.core.code_index.schema.items import CodeIndexResult
from ember_code.core.tools.codeindex.disambiguation import DisambiguationService


def _edge(from_uuid: str, to_uuid: str, relation: str, **meta) -> SimpleNamespace:
    return SimpleNamespace(
        from_uuid=from_uuid,
        to_uuid=to_uuid,
        relation=relation,
        meta=meta,
    )


def _item(
    item_id: str,
    *,
    parent_id: str = "",
    name: str = "",
    path: str = "",
) -> CodeIndexResult:
    return CodeIndexResult(
        item_id=item_id,
        parent_id=parent_id,
        name=name,
        path=path,
        commit="c1",
    )


def _build_service(
    *,
    direct_edges: list,
    parent_edges: list | None = None,
    parent_items: list[CodeIndexResult] | None = None,
) -> DisambiguationService:
    """Build a service whose edge fetches return ``direct_edges`` on
    the first call and ``parent_edges`` (if any) on the second; and
    whose ``filter_items`` returns ``parent_items``.

    The fallback path makes a second ``get_by_uuids`` call with the
    parent ids, so we use ``side_effect`` to return different results
    in sequence.
    """
    idx = MagicMock()
    file_ref_service = MagicMock()
    edge_responses = [direct_edges]
    if parent_edges is not None:
        edge_responses.append(parent_edges)
    file_ref_service.get_by_uuids = AsyncMock(side_effect=edge_responses)
    idx._file_reference_service = MagicMock(return_value=file_ref_service)

    # ``_rank_direction`` calls ``self._idx.search_among(candidate_ids=…)``
    # which returns ``CodeIndexResult``-shaped rows ranked against the
    # query. We stub it to echo back the candidates in order so the
    # group's ``calls`` / ``called_by`` lists end up populated and
    # ``refs_for`` doesn't skip them.
    async def _search_among_stub(*, query: str, candidate_ids: list[str], **_):
        return [
            CodeIndexResult(
                item_id=cid, name=f"{cid}_name", path=f"src/{cid}.py", content="", commit="c1"
            )
            for cid in candidate_ids
        ]

    idx.search_among = _search_among_stub
    # ``_fetch_parent_info`` calls ``self._idx.filter_items``.
    idx.filter_items = AsyncMock(return_value=parent_items or [])
    return DisambiguationService(idx)


# ── Empty input ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refs_for_empty_items_returns_none() -> None:
    service = _build_service(direct_edges=[])
    assert await service.refs_for(items=[], query_text="x", sha="c1") is None


@pytest.mark.asyncio
async def test_refs_for_items_without_ids_returns_none() -> None:
    service = _build_service(direct_edges=[])
    result = await service.refs_for(items=[_item("")], query_text="x", sha="c1")
    assert result is None


# ── Direct-edge path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refs_for_direct_edge_returns_group() -> None:
    """An item with direct outgoing edges gets a group with calls
    populated; no via_parent tag."""
    service = _build_service(
        direct_edges=[
            _edge("A", "B", str(Relation.CALLS), to_entity_name="B_name"),
        ]
    )
    items = [_item("A", parent_id="P_A")]
    result = await service.refs_for(items=items, query_text="x", sha="c1")
    assert result is not None
    assert "A" in result
    group = result["A"]
    assert group.via_parent is None
    # B is in A's calls direction; the exact rank depends on stubbed
    # ``search`` returning nothing, so target_meta is the source.
    assert group.calls or group.called_by  # something landed


# ── Parent-fallback path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_refs_for_parent_fallback_when_no_direct_edges() -> None:
    """An item with no direct edges falls through to its parent's
    edges and tags the resulting group with ``via_parent``."""
    parent_meta = {
        "to_entity_name": "ParentTarget",
        "to_entity_path": "src/p.py",
    }
    # Direct-edge fetch returns nothing for item "A". Parent fetch
    # returns an edge for parent "P_A" → "X".
    service = _build_service(
        direct_edges=[],
        parent_edges=[
            _edge("P_A", "X", str(Relation.CALLS), **parent_meta),
        ],
        parent_items=[_item("P_A", name="ParentClass", path="src/p.py")],
    )
    items = [_item("A", parent_id="P_A")]
    result = await service.refs_for(items=items, query_text="x", sha="c1")
    assert result is not None
    assert "A" in result
    group = result["A"]
    # via_parent is the human-readable label, not a truncated id.
    assert group.via_parent == "ParentClass (src/p.py)"


@pytest.mark.asyncio
async def test_refs_for_parent_fallback_emits_full_uuid_when_info_missing() -> None:
    """The cold path the truncated-id bug lived on: parent metadata
    fetch returned nothing. The fallback label must be the FULL
    parent_id so the agent can still query for it — not a truncated
    8-char stub."""
    parent_id = "deadbeefcafebabefeed0123456789ab"  # 32-char UUID-shape
    service = _build_service(
        direct_edges=[],
        parent_edges=[
            _edge(parent_id, "X", str(Relation.CALLS)),
        ],
        parent_items=[],  # parent metadata lookup yields nothing
    )
    items = [_item("A", parent_id=parent_id)]
    result = await service.refs_for(items=items, query_text="x", sha="c1")
    assert result is not None
    assert "A" in result
    assert result["A"].via_parent == parent_id, (
        f"expected full parent_id, got {result['A'].via_parent!r}"
    )


@pytest.mark.asyncio
async def test_refs_for_no_fallback_when_item_has_direct_edges() -> None:
    """If an item already has direct edges, the parent fallback must
    NOT fire for it — otherwise the group's via_parent tag would
    incorrectly override a direct relationship."""
    service = _build_service(
        direct_edges=[
            _edge("A", "B", str(Relation.CALLS)),
        ]
    )
    items = [_item("A", parent_id="P_A")]
    result = await service.refs_for(items=items, query_text="x", sha="c1")
    assert result is not None
    assert result["A"].via_parent is None
    # Only one ``get_by_uuids`` call should have fired — the direct one.
    fetch = service._idx._file_reference_service().get_by_uuids
    assert fetch.call_count == 1


@pytest.mark.asyncio
async def test_refs_for_item_without_parent_id_skipped_in_fallback() -> None:
    """Items with no parent_id can't fall back; they get no group
    rather than a group tagged with empty parent info."""
    service = _build_service(direct_edges=[])
    items = [_item("A", parent_id="")]
    result = await service.refs_for(items=items, query_text="x", sha="c1")
    # Either None or empty dict — both indicate "no refs."
    assert result is None or result == {}


@pytest.mark.asyncio
async def test_refs_for_mixed_direct_and_fallback() -> None:
    """A query can return multiple items where some have direct edges
    and others need the parent fallback. The result must include both
    kinds, each tagged appropriately."""
    service = _build_service(
        direct_edges=[
            # Item "A" has a direct edge; item "B" has none.
            _edge("A", "X", str(Relation.CALLS)),
        ],
        parent_edges=[
            # Parent of B has an edge.
            _edge("P_B", "Y", str(Relation.CALLS)),
        ],
        parent_items=[_item("P_B", name="BParent", path="src/b.py")],
    )
    items = [
        _item("A", parent_id="P_A"),
        _item("B", parent_id="P_B"),
    ]
    result = await service.refs_for(items=items, query_text="x", sha="c1")
    assert result is not None
    assert "A" in result
    assert "B" in result
    assert result["A"].via_parent is None
    assert result["B"].via_parent == "BParent (src/b.py)"


# ── No edges anywhere ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refs_for_no_direct_and_no_parent_edges_returns_none() -> None:
    """When neither direct nor parent edges exist, the method returns
    None so the toolkit drops the refs field entirely (exclude_none)."""
    service = _build_service(direct_edges=[], parent_edges=[])
    items = [_item("A", parent_id="P_A")]
    result = await service.refs_for(items=items, query_text="x", sha="c1")
    assert result is None
