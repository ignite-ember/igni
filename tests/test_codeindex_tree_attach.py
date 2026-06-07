"""Unit tests for ``tree_service._attach_references``.

The audit + my disambiguation fix flagged a related bug here: when
the item is the ``to_uuid`` of an edge, the relation's semantic
meaning has flipped from the agent's perspective (e.g., ``(Z, X,
CALLS)`` means Z calls X, so from X's POV the relation is
``CALLED_BY``). The old code bucketed by the raw stored relation,
which silently inverted the direction whenever the indexer's
mirrored pair was incomplete. Plus: dedupe was missing entirely,
so the mirror pair produced duplicate references in normal
operation.

These tests use a mocked ``FileReferenceService`` so the edge graph
is fully controllable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.code_index.enums import Relation
from ember_code.core.code_index.schema.items import CodeIndexResult
from ember_code.core.tools.codeindex.tree_service import TreeService


def _edge(from_uuid: str, to_uuid: str, relation: str, **meta) -> SimpleNamespace:
    return SimpleNamespace(
        from_uuid=from_uuid,
        to_uuid=to_uuid,
        relation=relation,
        meta=meta,
    )


def _make_service(edges: list) -> tuple[TreeService, MagicMock]:
    idx = MagicMock()
    file_ref_service = MagicMock()
    file_ref_service.get_by_uuids = AsyncMock(return_value=edges)
    idx._file_reference_service = MagicMock(return_value=file_ref_service)
    # Stub the chroma fetch used by _hydrate_target_summaries so the
    # method returns silently. We're testing bucketing, not hydration.
    idx.filter_items = AsyncMock(return_value=[])
    return TreeService(idx), file_ref_service


def _item(uuid_: str = "X") -> CodeIndexResult:
    return CodeIndexResult(item_id=uuid_, commit="c1")


# ── from-side bucketing (was already correct) ─────────────────────


@pytest.mark.asyncio
async def test_from_side_calls_buckets_target_into_calls() -> None:
    """Edge ``(X, Y, CALLS)`` with X as the item: X calls Y, so Y
    lands in X's ``calls`` list."""
    service, _ = _make_service(
        [
            _edge("X", "Y", str(Relation.CALLS), to_entity_name="Y_name", to_entity_path="Y/path"),
        ]
    )
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is not None
    assert str(Relation.CALLS) in item.references
    assert item.references[str(Relation.CALLS)][0].id == "Y"
    assert item.references[str(Relation.CALLS)][0].name == "Y_name"


@pytest.mark.asyncio
async def test_from_side_called_by_buckets_into_called_by() -> None:
    """Edge ``(X, Y, CALLED_BY)`` with X as the item: X is called by
    Y, so Y lands in X's ``called_by`` list."""
    service, _ = _make_service(
        [
            _edge("X", "Y", str(Relation.CALLED_BY)),
        ]
    )
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is not None
    assert str(Relation.CALLED_BY) in item.references
    assert item.references[str(Relation.CALLED_BY)][0].id == "Y"


# ── to-side bucketing (was the bug) ───────────────────────────────


@pytest.mark.asyncio
async def test_to_side_calls_inverts_to_called_by() -> None:
    """Edge ``(Z, X, CALLS)`` with X as the item: Z calls X, so from
    X's perspective Z is in ``called_by`` (NOT ``calls``).

    Pre-fix bug: this bucketed Z under ``calls``, telling the agent
    "X calls Z" — exactly inverting the call graph.
    """
    service, _ = _make_service(
        [
            _edge(
                "Z", "X", str(Relation.CALLS), from_entity_name="Z_name", from_entity_path="Z/path"
            ),
        ]
    )
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is not None
    # The relation key must be CALLED_BY (inverse), not CALLS.
    assert str(Relation.CALLED_BY) in item.references
    assert str(Relation.CALLS) not in item.references
    target = item.references[str(Relation.CALLED_BY)][0]
    assert target.id == "Z"
    assert target.name == "Z_name"


@pytest.mark.asyncio
async def test_to_side_called_by_inverts_to_calls() -> None:
    """Edge ``(Z, X, CALLED_BY)`` with X as the item: Z is called by
    X, so from X's perspective X calls Z — Z is in ``calls``."""
    service, _ = _make_service(
        [
            _edge("Z", "X", str(Relation.CALLED_BY)),
        ]
    )
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is not None
    assert str(Relation.CALLS) in item.references
    assert str(Relation.CALLED_BY) not in item.references


@pytest.mark.asyncio
async def test_to_side_imports_inverts_to_imported_by() -> None:
    """Direction-inversion must work for every relation pair, not
    just CALLS/CALLED_BY. Covers the IMPORTS family."""
    service, _ = _make_service(
        [
            _edge("Z", "X", str(Relation.IMPORTS)),
        ]
    )
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is not None
    assert str(Relation.IMPORTED_BY) in item.references
    assert str(Relation.IMPORTS) not in item.references


# ── Mirror dedupe ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mirror_pair_dedupes_to_single_reference() -> None:
    """The normal case: the indexer mirrors every edge. We observe X
    once as the from-side (``(X, Y, CALLS)``) and once as the to-side
    of the mirror (``(Y, X, CALLED_BY)``). Pre-fix, the agent saw Y
    twice — once under ``calls``, once under ``called_by`` — implying
    a mutual recursion. With the inverse-flip + dedupe both
    observations land in ``calls`` and collapse to one entry."""
    service, _ = _make_service(
        [
            _edge("X", "Y", str(Relation.CALLS), to_entity_name="Y_name"),
            _edge("Y", "X", str(Relation.CALLED_BY), from_entity_name="Y_name"),
        ]
    )
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is not None
    # Both observations describe "X calls Y" — Y lands in CALLS once.
    assert len(item.references[str(Relation.CALLS)]) == 1
    assert item.references[str(Relation.CALLS)][0].id == "Y"
    # No entry under the inverse direction.
    assert str(Relation.CALLED_BY) not in item.references


@pytest.mark.asyncio
async def test_mixed_call_graph_routes_each_side_correctly() -> None:
    """Realistic mix: X calls Y AND Z calls X. Each direction lands
    in the right bucket and the mirror duplicates dedupe out."""
    service, _ = _make_service(
        [
            # X calls Y — observed from both sides of the mirror.
            _edge("X", "Y", str(Relation.CALLS)),
            _edge("Y", "X", str(Relation.CALLED_BY)),
            # Z calls X — observed from both sides.
            _edge("Z", "X", str(Relation.CALLS)),
            _edge("X", "Z", str(Relation.CALLED_BY)),
        ]
    )
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is not None
    calls_ids = {t.id for t in item.references[str(Relation.CALLS)]}
    called_by_ids = {t.id for t in item.references[str(Relation.CALLED_BY)]}
    assert calls_ids == {"Y"}
    assert called_by_ids == {"Z"}


# ── Other edge cases ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_neither_endpoint_match_skips_edge() -> None:
    """If neither endpoint matches the item id, the edge is unrelated
    and skipped — not an error, just a no-op."""
    service, _ = _make_service(
        [
            _edge("Q", "R", str(Relation.CALLS)),  # nothing to do with X
        ]
    )
    item = _item("X")
    await service._attach_references(item, relations=None)
    # No edges matched → references stays None (default).
    assert item.references is None


@pytest.mark.asyncio
async def test_empty_edges_leaves_references_none() -> None:
    service, _ = _make_service([])
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is None


@pytest.mark.asyncio
async def test_unknown_relation_logs_and_passes_through() -> None:
    """A new Relation enum value not in ``_INVERSE_RELATION`` falls
    back to bucketing as-is and logs a warning. Better than silently
    losing the edge; the log alerts us to update the inverse map.

    We patch the module's ``logger`` directly to assert the warning
    fires — pytest's ``caplog`` is unreliable here because other
    tests in the suite end up reconfiguring root logging in ways
    that mask warnings emitted on this module's logger.
    """
    from unittest.mock import patch

    service, _ = _make_service(
        [
            _edge("Z", "X", "invented_relation"),
        ]
    )
    item = _item("X")
    with patch("ember_code.core.tools.codeindex.tree_service.logger.warning") as warn:
        await service._attach_references(item, relations=None)

    assert item.references is not None
    assert "invented_relation" in item.references
    # Exactly one warning fired, mentioning the missing inverse.
    assert warn.called, "expected logger.warning to fire on unknown relation"
    args, _kwargs = warn.call_args
    assert "no inverse known" in args[0].lower()


@pytest.mark.asyncio
async def test_fetch_exception_returns_silently() -> None:
    """Edge-fetch failure must not raise — references stays None."""
    idx = MagicMock()
    file_ref_service = MagicMock()
    file_ref_service.get_by_uuids = AsyncMock(side_effect=RuntimeError("boom"))
    idx._file_reference_service = MagicMock(return_value=file_ref_service)
    service = TreeService(idx)
    item = _item("X")
    await service._attach_references(item, relations=None)
    assert item.references is None


@pytest.mark.asyncio
async def test_missing_item_id_returns_early() -> None:
    """An item with no item_id can't have edges fetched; the method
    should return silently without invoking the file ref service."""
    service, file_ref_service = _make_service([])
    item = CodeIndexResult(item_id="", commit="c1")
    await service._attach_references(item, relations=None)
    file_ref_service.get_by_uuids.assert_not_called()
