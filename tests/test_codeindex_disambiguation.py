"""Unit tests for ``core/tools/codeindex/disambiguation.py``.

The disambiguation service was 0% unit-tested before this file. The
audit flagged the 4-branch ``_collect_edges`` bucketing as the
likeliest source of silent regressions — especially the two
"symmetric inverse" branches that turned out to *mis-bucket* edges
into the wrong direction when the indexer's mirrored pair was
incomplete.

We test the bucketing logic in isolation by mocking the index's
edge fetch. The real chroma layer is not exercised here — that's
covered by ``test_codeindex_eval_fixture.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.code_index.enums import Relation
from ember_code.core.code_index.schema.items import CodeIndexResult
from ember_code.core.tools.codeindex.disambiguation import DisambiguationService


def _edge(from_uuid: str, to_uuid: str, relation: str, **meta) -> SimpleNamespace:
    """Build the minimum edge object the bucketing reads."""
    return SimpleNamespace(
        from_uuid=from_uuid,
        to_uuid=to_uuid,
        relation=relation,
        meta=meta,
    )


def _make_service(edges: list) -> DisambiguationService:
    """Build a DisambiguationService whose edge fetch returns ``edges``."""
    idx = MagicMock()
    file_ref_service = MagicMock()
    file_ref_service.get_by_uuids = AsyncMock(return_value=edges)
    idx.file_reference_service = MagicMock(return_value=file_ref_service)
    return DisambiguationService(idx)


# ── _collect_edges direction handling ──────────────────────────────


@pytest.mark.asyncio
async def test_collect_edges_outgoing_buckets_into_calls() -> None:
    """A CALLS edge ``(from=A, to=B)`` with A in batch buckets B into
    A's calls list."""
    service = _make_service(
        [
            _edge(
                "A",
                "B",
                str(Relation.CALLS),
                from_entity_name="A_name",
                from_entity_path="A/path",
                to_entity_name="B_name",
                to_entity_path="B/path",
            ),
        ]
    )
    per_item, target_meta = await service._collect_edges(["A"])
    assert per_item["A"]["calls"] == ["B"]
    assert per_item["A"]["called_by"] == []
    assert target_meta["B"]["name"] == "B_name"


@pytest.mark.asyncio
async def test_collect_edges_incoming_buckets_into_called_by() -> None:
    """A CALLED_BY edge ``(from=A, to=B)`` (semantics: A is called by B)
    with A in batch buckets B into A's called_by list."""
    service = _make_service(
        [
            _edge(
                "A",
                "B",
                str(Relation.CALLED_BY),
                from_entity_name="A_name",
                from_entity_path="A/path",
                to_entity_name="B_name",
                to_entity_path="B/path",
            ),
        ]
    )
    per_item, target_meta = await service._collect_edges(["A"])
    assert per_item["A"]["called_by"] == ["B"]
    assert per_item["A"]["calls"] == []
    assert target_meta["B"]["name"] == "B_name"


@pytest.mark.asyncio
async def test_collect_edges_mirrored_pair_dedupes_naturally() -> None:
    """When BOTH a CALLS and the inverse CALLED_BY are stored (the
    indexer's normal pattern), each side ends up in the right bucket.
    The bug we fixed was: the legacy "symmetric inverse" branches
    used to *also* fire on the wrong side and mis-bucket — leading to
    the same edge appearing in both calls AND called_by for one
    item."""
    service = _make_service(
        [
            _edge("A", "B", str(Relation.CALLS)),
            _edge("B", "A", str(Relation.CALLED_BY)),
        ]
    )
    per_item, _ = await service._collect_edges(["A"])
    # A is the caller in both edges; only its calls list should mention B.
    assert per_item["A"]["calls"] == ["B"]
    # A's called_by list must NOT contain B — that would be the bug.
    assert per_item["A"]["called_by"] == []


@pytest.mark.asyncio
async def test_collect_edges_skips_self_loops() -> None:
    """Self-loops aren't useful for disambiguation — they shouldn't
    show up in any bucket."""
    service = _make_service(
        [
            _edge("A", "A", str(Relation.CALLS)),
            _edge("A", "A", str(Relation.CALLED_BY)),
        ]
    )
    per_item, _ = await service._collect_edges(["A"])
    assert per_item["A"]["calls"] == []
    assert per_item["A"]["called_by"] == []


@pytest.mark.asyncio
async def test_collect_edges_to_side_in_batch_calls_edge() -> None:
    """An edge ``(A, B, CALLS)`` where only B (the callee) is in the
    batch must bucket A into B's ``called_by`` list — NOT into B's
    ``calls`` list (B doesn't call A; A calls B).

    Pre-fix bug: the legacy "to_uuid in per_item" branch wrote into
    the wrong bucket, silently inverting the direction whenever the
    indexer's mirrored CALLED_BY edge wasn't also present.
    """
    service = _make_service(
        [
            _edge(
                "A", "B", str(Relation.CALLS), from_entity_name="A_name", from_entity_path="A/path"
            ),
        ]
    )
    per_item, target_meta = await service._collect_edges(["B"])
    assert per_item["B"]["called_by"] == ["A"], (
        "B should have A in called_by (A calls B), not in calls"
    )
    assert per_item["B"]["calls"] == []
    assert target_meta["A"]["name"] == "A_name"


@pytest.mark.asyncio
async def test_collect_edges_to_side_in_batch_called_by_edge() -> None:
    """Symmetric counterpart: an edge ``(A, B, CALLED_BY)`` (A is
    called by B) with only B in the batch must bucket A into B's
    ``calls`` list — NOT into B's ``called_by`` (B is the caller)."""
    service = _make_service(
        [
            _edge(
                "A",
                "B",
                str(Relation.CALLED_BY),
                from_entity_name="A_name",
                from_entity_path="A/path",
            ),
        ]
    )
    per_item, target_meta = await service._collect_edges(["B"])
    assert per_item["B"]["calls"] == ["A"], (
        "B should have A in calls (A is called by B → B calls A), not in called_by"
    )
    assert per_item["B"]["called_by"] == []
    assert target_meta["A"]["name"] == "A_name"


@pytest.mark.asyncio
async def test_collect_edges_dedupes_mirrored_observations() -> None:
    """When both endpoints are in the batch, the mirrored CALLS +
    CALLED_BY pair is observed from both sides — A's perspective on
    CALLS, B's perspective on CALLED_BY, plus the to-side branches
    on each. Dedup must collapse the duplicates."""
    service = _make_service(
        [
            _edge("A", "B", str(Relation.CALLS)),
            _edge("B", "A", str(Relation.CALLED_BY)),
        ]
    )
    per_item, _ = await service._collect_edges(["A", "B"])
    # A's calls list contains B once (not twice).
    assert per_item["A"]["calls"] == ["B"]
    # B's called_by list contains A once.
    assert per_item["B"]["called_by"] == ["A"]
    # And the wrong-direction buckets are empty.
    assert per_item["A"]["called_by"] == []
    assert per_item["B"]["calls"] == []


@pytest.mark.asyncio
async def test_collect_edges_drops_edge_with_neither_endpoint_in_batch() -> None:
    """An edge whose neither endpoint is in our query batch is legitimate
    to drop — the agent didn't ask about either entity. We just confirm
    the drop doesn't crash and doesn't pollute per_item."""
    service = _make_service(
        [
            _edge("X", "Y", str(Relation.CALLS)),  # X, Y not in batch
        ]
    )
    per_item, target_meta = await service._collect_edges(["A"])
    assert per_item == {"A": {"calls": [], "called_by": []}}
    assert target_meta == {}


@pytest.mark.asyncio
async def test_collect_edges_unknown_relation_logs_warning() -> None:
    """A relation value not in OUTGOING/INCOMING was silently dropping
    edges before. Now it must log a warning so the gap surfaces — a
    new Relation enum value added without updating the constants gets
    flagged.

    We patch the module's logger directly because ``caplog`` is
    unreliable here — other tests in the suite reconfigure root
    logging in ways that mask the warning emitted on this module's
    logger.
    """
    from unittest.mock import patch

    service = _make_service(
        [
            _edge("A", "B", "totally_invented_relation"),
        ]
    )
    with patch("ember_code.core.tools.codeindex.disambiguation.logger.warning") as warn:
        per_item, _ = await service._collect_edges(["A"])
    assert per_item == {"A": {"calls": [], "called_by": []}}
    assert warn.called
    args, _kwargs = warn.call_args
    assert "unknown relation" in args[0].lower()


@pytest.mark.asyncio
async def test_collect_edges_multiple_targets_in_calls_list() -> None:
    """A single source can call many targets — each goes into the calls list."""
    service = _make_service(
        [
            _edge("A", "B", str(Relation.CALLS)),
            _edge("A", "C", str(Relation.CALLS)),
            _edge("A", "D", str(Relation.CALLS)),
        ]
    )
    per_item, _ = await service._collect_edges(["A"])
    assert sorted(per_item["A"]["calls"]) == ["B", "C", "D"]


@pytest.mark.asyncio
async def test_collect_edges_mixed_relation_types_per_item() -> None:
    """One item can have several different OUTGOING relations
    (CALLS, IMPORTS, EXTENDS) — all bucket into the same calls list,
    since for disambiguation purposes the direction matters more than
    the specific relation flavor."""
    service = _make_service(
        [
            _edge("A", "B", str(Relation.CALLS)),
            _edge("A", "C", str(Relation.IMPORTS)),
            _edge("A", "D", str(Relation.EXTENDS)),
        ]
    )
    per_item, _ = await service._collect_edges(["A"])
    assert sorted(per_item["A"]["calls"]) == ["B", "C", "D"]


@pytest.mark.asyncio
async def test_collect_edges_returns_none_on_empty_fetch() -> None:
    service = _make_service([])
    per_item, target_meta = await service._collect_edges(["A"])
    # No edges at all — service returns (None, {}) sentinel.
    assert per_item is None
    assert target_meta == {}


@pytest.mark.asyncio
async def test_collect_edges_returns_none_on_fetch_exception() -> None:
    """Edge fetch failure must not propagate; returns (None, {})."""
    idx = MagicMock()
    file_ref_service = MagicMock()
    file_ref_service.get_by_uuids = AsyncMock(side_effect=RuntimeError("boom"))
    idx.file_reference_service = MagicMock(return_value=file_ref_service)
    service = DisambiguationService(idx)

    per_item, target_meta = await service._collect_edges(["A"])
    assert per_item is None
    assert target_meta == {}


@pytest.mark.asyncio
async def test_build_group_dedupes_self_loops_between_directions() -> None:
    """``_build_group`` collapses the indexer's symmetric pairs: when
    the same target shows up in both ``calls`` and ``called_by`` for
    one item (because both the CALLS and the mirrored CALLED_BY edge
    were observed), keep the ``called_by`` copy and drop the duplicate
    from ``calls``. ``called_by`` carries the more discriminating
    "who uses me" signal for disambiguation."""
    idx = MagicMock()

    async def _search_among_stub(*, query, candidate_ids, **_):
        # Echo candidates back in order with name/path metadata.
        return [
            CodeIndexResult(
                item_id=cid,
                name=f"{cid}_n",
                path=f"{cid}.py",
                content="",
                commit="c1",
            )
            for cid in candidate_ids
        ]

    idx.search_among = _search_among_stub
    service = DisambiguationService(idx)

    group = await service._build_group(
        dirs={"calls": ["B", "C"], "called_by": ["B"]},  # B is the dup
        target_meta={
            "B": {"name": "B_name", "path": "src/b.py"},
            "C": {"name": "C_name", "path": "src/c.py"},
        },
        query_text="x",
        sha="c1",
    )
    # B kept in called_by; C kept in calls; B's duplicate in calls
    # stripped out.
    called_by_ids = {r.item_id for r in group.called_by}
    calls_ids = {r.item_id for r in group.calls}
    assert called_by_ids == {"B"}
    assert calls_ids == {"C"}  # B removed because it appears in called_by


@pytest.mark.asyncio
async def test_rank_direction_returns_empty_when_no_candidates() -> None:
    """No candidates → empty list, no exception."""
    idx = MagicMock()
    idx.search_among = AsyncMock(return_value=[])
    service = DisambiguationService(idx)
    refs = await service._rank_direction(target_ids=[], query_text="x", sha="c1", target_meta={})
    assert refs == []


@pytest.mark.asyncio
async def test_rank_direction_handles_search_exception() -> None:
    """If chroma's ``search_among`` fails, return [] rather than
    propagating — refs are best-effort enrichment, not core
    functionality."""
    idx = MagicMock()
    idx.search_among = AsyncMock(side_effect=RuntimeError("chroma down"))
    service = DisambiguationService(idx)
    refs = await service._rank_direction(
        target_ids=["A", "B"], query_text="x", sha="c1", target_meta={}
    )
    assert refs == []


@pytest.mark.asyncio
async def test_rank_direction_falls_back_to_target_meta_for_name_path() -> None:
    """When ``search_among`` returns a row whose ``name``/``path``
    fields are empty (e.g. the chroma row was evicted), the ranker
    pulls the name/path from the ``target_meta`` cache populated
    earlier by ``_collect_edges`` — the agent still gets a useful
    label rather than blanks."""
    from ember_code.core.code_index.schema.items import CodeIndexResult

    idx = MagicMock()
    idx.search_among = AsyncMock(
        return_value=[
            # Result row has empty name/path but a valid item_id.
            CodeIndexResult(item_id="A", name="", path="", content="", commit="c1"),
        ]
    )
    service = DisambiguationService(idx)
    refs = await service._rank_direction(
        target_ids=["A"],
        query_text="x",
        sha="c1",
        target_meta={"A": {"name": "fallback_name", "path": "fallback/path"}},
    )
    assert len(refs) == 1
    assert refs[0].name == "fallback_name"
    assert refs[0].path == "fallback/path"


@pytest.mark.asyncio
async def test_rank_direction_dedupes_target_ids() -> None:
    """Duplicate ids in ``target_ids`` (rare, but possible if
    upstream bucketing produced them) get deduped before the
    search call so we don't waste re-ranking on the same uuid."""
    from ember_code.core.code_index.schema.items import CodeIndexResult

    captured: list = []

    async def _search_among_stub(*, query, candidate_ids, **_):
        captured.append(list(candidate_ids))
        return [
            CodeIndexResult(item_id=cid, name=cid, path=f"{cid}.py", content="", commit="c1")
            for cid in candidate_ids
        ]

    idx = MagicMock()
    idx.search_among = _search_among_stub
    service = DisambiguationService(idx)
    await service._rank_direction(
        target_ids=["A", "A", "B", "A"],
        query_text="x",
        sha="c1",
        target_meta={},
    )
    # ``candidate_ids`` was deduped to {"A", "B"}.
    assert captured, "expected search_among to be called once"
    assert set(captured[0]) == {"A", "B"}
    assert len(captured[0]) == 2


@pytest.mark.asyncio
async def test_collect_edges_caches_target_meta_for_each_target() -> None:
    """The target_meta dict accumulates name/path info for every UUID
    we surface, so refs whose chroma row no longer exists can still be
    rendered with a useful label."""
    service = _make_service(
        [
            _edge(
                "A",
                "B",
                str(Relation.CALLS),
                to_entity_name="B_func",
                to_entity_path="src/foo.py::B_func",
            ),
            _edge(
                "A",
                "C",
                str(Relation.IMPORTS),
                to_entity_name="C_mod",
                to_entity_path="src/lib/c.py",
            ),
        ]
    )
    _, target_meta = await service._collect_edges(["A"])
    assert target_meta["B"]["name"] == "B_func"
    assert target_meta["B"]["path"] == "src/foo.py::B_func"
    assert target_meta["C"]["name"] == "C_mod"
    assert target_meta["C"]["path"] == "src/lib/c.py"
