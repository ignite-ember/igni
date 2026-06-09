"""Tests for the JSONL delta contract + applier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ember_code.core.code_index.delta import (
    CommitOp,
    DeltaError,
    UpsertItemOp,
    apply_delta,
    iter_ops,
    parse_op,
)
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.paths import state_db_path
from ember_code.core.code_index.pg.file_reference import FileReferenceService
from ember_code.core.db.database import Database


@pytest.fixture
async def index(tmp_path):
    idx = CodeIndex(project=tmp_path / "proj_a", data_dir=str(tmp_path / "data"))
    yield idx
    await idx.close()


@pytest.fixture
def file_refs(tmp_path):
    db = Database(state_db_path(tmp_path / "proj_a", data_dir=str(tmp_path / "data")))
    return FileReferenceService(db)


def _write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


# -- Parsing ------------------------------------------------------------------


class TestParseOp:
    def test_blank_line_returns_none(self):
        assert parse_op("") is None
        assert parse_op("   ") is None

    def test_invalid_json_raises(self):
        with pytest.raises(DeltaError, match="invalid JSON"):
            parse_op("{not json}")

    def test_missing_op_field_raises(self):
        with pytest.raises(DeltaError, match="missing 'op'"):
            parse_op(json.dumps({"id": "x"}))

    def test_unknown_op_raises(self):
        with pytest.raises(DeltaError, match="unknown op"):
            parse_op(json.dumps({"op": "rename"}))

    def test_validation_error_on_bad_payload(self):
        with pytest.raises(DeltaError, match="validation failed"):
            parse_op(json.dumps({"op": "upsert_item"}))

    def test_commit_op_parses(self):
        op = parse_op(
            json.dumps({"op": "commit", "sha": "abc", "parent_sha": None, "branches": []})
        )
        assert isinstance(op, CommitOp)
        assert op.sha == "abc"

    def test_upsert_item_parses(self):
        op = parse_op(
            json.dumps(
                {
                    "op": "upsert_item",
                    "id": "a",
                    "type": "file",
                    "name": "x.py",
                    "path": "src/x.py",
                    "content": "...",
                    "tags": ["type:file"],
                }
            )
        )
        assert isinstance(op, UpsertItemOp)
        assert op.path == "src/x.py"

    def test_iter_ops_skips_blanks(self, tmp_path):
        path = tmp_path / "delta.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"op": "commit", "sha": "abc"}),
                    "",
                    json.dumps({"op": "delete_item", "id": "a"}),
                ]
            )
        )
        ops = list(iter_ops(path))
        assert [type(o).__name__ for o in ops] == ["CommitOp", "DeleteItemOp"]


# -- Apply --------------------------------------------------------------------


class TestApplyDelta:
    @pytest.mark.asyncio
    async def test_first_line_must_be_commit(self, index, file_refs, tmp_path):
        path = _write_jsonl(tmp_path / "delta.jsonl", [{"op": "delete_item", "id": "x"}])
        with pytest.raises(DeltaError, match="first line"):
            await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)

    @pytest.mark.asyncio
    async def test_empty_file_raises(self, index, file_refs, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        with pytest.raises(DeltaError, match="empty"):
            await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)

    @pytest.mark.asyncio
    async def test_on_progress_fires_with_item_counts(self, index, file_refs, tmp_path):
        """The progress callback must receive ``(done, total, label)``
        for each upserted item, with ``total`` matching the up-front
        item count (refs and commit ops excluded). This drives the
        ``Resyncing N/M`` busy label during ``/codeindex resync``."""
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s", "parent_sha": None},
                {
                    "op": "upsert_item",
                    "id": "a",
                    "type": "file",
                    "name": "a.py",
                    "path": "a.py",
                    "content": "alpha",
                    "kind": "code",
                },
                {
                    "op": "upsert_item",
                    "id": "b",
                    "type": "file",
                    "name": "b.py",
                    "path": "b.py",
                    "content": "beta",
                    "kind": "code",
                },
                # References are cheap, they must NOT inflate the bar.
                {
                    "op": "upsert_reference",
                    "from_id": "a",
                    "to_id": "b",
                    "relation": "imports",
                    "meta": {},
                },
            ],
        )
        calls: list[tuple[int, int, str]] = []
        await apply_delta(
            index=index,
            file_refs=file_refs,
            jsonl_path=path,
            on_progress=lambda done, total, label: calls.append((done, total, label)),
        )

        assert calls, "callback must fire at least once"
        # Every call agrees on the same total (the up-front item count).
        totals = {t for _, t, _ in calls}
        assert totals == {2}
        # ``done`` is monotonic and ends at total.
        dones = [d for d, _, _ in calls]
        assert dones == sorted(dones)
        assert dones[-1] == 2
        # The opening "preparing" call carries done=0; subsequent calls
        # carry a non-empty label (the item path).
        assert calls[0][0] == 0
        assert calls[0][2] == "preparing"
        # Item-level labels include the path strings.
        item_labels = [label for done, _, label in calls if done > 0]
        assert "a.py" in item_labels
        assert "b.py" in item_labels

    @pytest.mark.asyncio
    async def test_on_progress_swallows_exceptions(self, index, file_refs, tmp_path):
        """A misbehaving callback must not abort the apply — progress
        is a UI nicety, not a load-bearing part of the indexing path."""
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s", "parent_sha": None},
                {
                    "op": "upsert_item",
                    "id": "a",
                    "type": "file",
                    "name": "a.py",
                    "path": "a.py",
                    "content": "alpha",
                    "kind": "code",
                },
            ],
        )

        def boom(done: int, total: int, label: str) -> None:
            raise RuntimeError("UI exploded")

        stats = await apply_delta(
            index=index,
            file_refs=file_refs,
            jsonl_path=path,
            on_progress=boom,
        )
        assert stats.items_upserted == 1

    @pytest.mark.asyncio
    async def test_full_round_trip(self, index, file_refs, tmp_path):
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "head_sha", "parent_sha": None},
                {
                    "op": "upsert_item",
                    "id": "auth-uuid",
                    "type": "file",
                    "name": "auth.py",
                    "path": "src/auth.py",
                    "content": "JWT authentication issues access tokens.",
                    "kind": "code",
                    "file_extension": "py",
                },
                {
                    "op": "upsert_item",
                    "id": "user-uuid",
                    "type": "file",
                    "name": "user.py",
                    "path": "src/user.py",
                    "content": "User profile management.",
                    "kind": "code",
                    "file_extension": "py",
                },
                {
                    "op": "upsert_reference",
                    "from_id": "auth-uuid",
                    "to_id": "user-uuid",
                    "relation": "imports",
                    "meta": {"line": 5},
                },
            ],
        )
        stats = await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)
        assert stats.items_upserted == 2
        assert stats.references_upserted == 1
        assert stats.items_deleted == 0

        # Items landed in the chroma file for the named commit.
        item = await index.get_item("auth-uuid")
        assert item is not None and item.name == "auth.py"

        # head pointer set.
        assert index.head() == "head_sha"

        # Reference landed in SQLite.
        ref = await file_refs.get(from_uuid="auth-uuid", to_uuid="user-uuid", relation="imports")
        assert ref is not None
        assert ref.relation == "imports"

    @pytest.mark.asyncio
    async def test_idempotent_when_replayed(self, index, file_refs, tmp_path):
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                {
                    "op": "upsert_item",
                    "id": "i1",
                    "type": "file",
                    "name": "a.py",
                    "path": "a.py",
                    "content": "alpha",
                },
            ],
        )
        await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)
        await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)

        # Replays don't double-up — same item is upserted in place.
        results = await index.search(query="alpha", limit=10)
        # Filter to the item we just added (chunk hits are deduped to the parent).
        matches = [r for r in results if r.item_id == "i1"]
        assert len(matches) == 1

    @pytest.mark.asyncio
    async def test_delete_item_drops_from_index(self, index, file_refs, tmp_path):
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                {
                    "op": "upsert_item",
                    "id": "i1",
                    "type": "file",
                    "name": "a.py",
                    "path": "a.py",
                    "content": "alpha",
                },
                {"op": "delete_item", "id": "i1"},
            ],
        )
        stats = await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)
        assert stats.items_upserted == 1
        assert stats.items_deleted == 1
        assert await index.get_item("i1") is None

    @pytest.mark.asyncio
    async def test_delete_reference(self, index, file_refs, tmp_path):
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                {
                    "op": "upsert_reference",
                    "from_id": "a",
                    "to_id": "b",
                    "relation": "calls",
                    "meta": {},
                },
                {"op": "delete_reference", "from_id": "a", "to_id": "b"},
            ],
        )
        stats = await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)
        assert stats.references_upserted == 1
        assert stats.references_deleted == 1
        assert await file_refs.get(from_uuid="a", to_uuid="b", relation="calls") is None

    @pytest.mark.asyncio
    async def test_copy_on_write_from_parent(self, index, file_refs, tmp_path):
        # First commit: seed an item.
        first = _write_jsonl(
            tmp_path / "first.jsonl",
            [
                {"op": "commit", "sha": "parent"},
                {
                    "op": "upsert_item",
                    "id": "shared",
                    "type": "file",
                    "name": "shared.py",
                    "path": "shared.py",
                    "content": "carried over from parent",
                },
            ],
        )
        await apply_delta(index=index, file_refs=file_refs, jsonl_path=first)

        # Second commit: empty, but declares parent. The shared item must
        # still be queryable in the child commit thanks to copy-on-write.
        second = _write_jsonl(
            tmp_path / "second.jsonl",
            [{"op": "commit", "sha": "child", "parent_sha": "parent"}],
        )
        await apply_delta(index=index, file_refs=file_refs, jsonl_path=second)
        assert index.head() == "child"
        item = await index.get_item("shared", commit="child")
        assert item is not None and item.name == "shared.py"

    @pytest.mark.asyncio
    async def test_second_commit_header_in_file_raises(self, index, file_refs, tmp_path):
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                {"op": "commit", "sha": "s2"},
            ],
        )
        with pytest.raises(DeltaError, match="second commit header"):
            await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)


class TestDeleteItemCascadesReferences:
    """``delete_item`` must scrub every reference (in or out) involving the
    deleted UUID. The cloud emitter does NOT send explicit
    ``delete_reference`` ops for the edges around an item it kills via
    ``delete_item`` — it relies on this client-side cascade. Without it
    the local file-references table grows orphan rows pointing at
    UUIDs that no longer exist in the index, which then show up in
    ``codeindex_tree`` and reverse-lookup results."""

    @pytest.mark.asyncio
    async def test_outgoing_reference_removed(self, index, file_refs, tmp_path):
        """``a → b`` edge, then ``a`` deleted → edge gone, ``b`` still alive."""
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                {
                    "op": "upsert_item",
                    "id": "a",
                    "type": "file",
                    "name": "a.py",
                    "path": "a.py",
                    "content": "A",
                },
                {
                    "op": "upsert_item",
                    "id": "b",
                    "type": "file",
                    "name": "b.py",
                    "path": "b.py",
                    "content": "B",
                },
                {
                    "op": "upsert_reference",
                    "from_id": "a",
                    "to_id": "b",
                    "relation": "imports",
                    "meta": {},
                },
                {"op": "delete_item", "id": "a"},
            ],
        )
        stats = await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)

        assert stats.items_deleted == 1
        # The cascade reports the reference we wiped, on top of any
        # explicit ``delete_reference`` ops in the file (none here).
        assert stats.references_deleted >= 1
        # Edge is gone (regardless of relation).
        assert await file_refs.get(from_uuid="a", to_uuid="b", relation="imports") is None
        # The OTHER endpoint isn't affected — it's a legitimate item
        # that just lost an inbound reference.
        assert await index.get_item("b") is not None

    @pytest.mark.asyncio
    async def test_incoming_reference_removed(self, index, file_refs, tmp_path):
        """``a → b`` edge, then ``b`` deleted → edge gone, ``a`` still alive.

        Incoming edges matter as much as outgoing — if ``b`` is removed
        and ``a → b`` survives, every reverse-lookup of ``b``'s callers
        still surfaces ``a`` as pointing at a phantom row."""
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                {
                    "op": "upsert_item",
                    "id": "a",
                    "type": "file",
                    "name": "a.py",
                    "path": "a.py",
                    "content": "A",
                },
                {
                    "op": "upsert_item",
                    "id": "b",
                    "type": "file",
                    "name": "b.py",
                    "path": "b.py",
                    "content": "B",
                },
                {
                    "op": "upsert_reference",
                    "from_id": "a",
                    "to_id": "b",
                    "relation": "imports",
                    "meta": {},
                },
                {"op": "delete_item", "id": "b"},
            ],
        )
        stats = await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)

        assert stats.items_deleted == 1
        assert stats.references_deleted >= 1
        assert await file_refs.get(from_uuid="a", to_uuid="b", relation="imports") is None
        assert await index.get_item("a") is not None

    @pytest.mark.asyncio
    async def test_multiple_edges_around_deleted_item_all_removed(self, index, file_refs, tmp_path):
        """Hub item ``x`` with edges to ``y1`` and ``y2`` and incoming edges
        from ``z1`` and ``z2``. Deleting ``x`` must remove all four edges."""
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                # Items
                *(
                    {
                        "op": "upsert_item",
                        "id": iid,
                        "type": "file",
                        "name": f"{iid}.py",
                        "path": f"{iid}.py",
                        "content": iid,
                    }
                    for iid in ("x", "y1", "y2", "z1", "z2")
                ),
                # Edges centered on x
                {
                    "op": "upsert_reference",
                    "from_id": "x",
                    "to_id": "y1",
                    "relation": "calls",
                    "meta": {},
                },
                {
                    "op": "upsert_reference",
                    "from_id": "x",
                    "to_id": "y2",
                    "relation": "calls",
                    "meta": {},
                },
                {
                    "op": "upsert_reference",
                    "from_id": "z1",
                    "to_id": "x",
                    "relation": "imports",
                    "meta": {},
                },
                {
                    "op": "upsert_reference",
                    "from_id": "z2",
                    "to_id": "x",
                    "relation": "imports",
                    "meta": {},
                },
                # An unrelated edge that should survive
                {
                    "op": "upsert_reference",
                    "from_id": "z1",
                    "to_id": "y1",
                    "relation": "calls",
                    "meta": {},
                },
                {"op": "delete_item", "id": "x"},
            ],
        )
        stats = await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)

        # 4 edges touched ``x``; the unrelated z1→y1 survives.
        assert stats.references_deleted == 4

        for from_uuid, to_uuid in (("x", "y1"), ("x", "y2"), ("z1", "x"), ("z2", "x")):
            assert (
                await file_refs.get(from_uuid=from_uuid, to_uuid=to_uuid, relation="calls") is None
            )
            assert (
                await file_refs.get(from_uuid=from_uuid, to_uuid=to_uuid, relation="imports")
                is None
            )

        # The unrelated edge is untouched.
        assert await file_refs.get(from_uuid="z1", to_uuid="y1", relation="calls") is not None

    @pytest.mark.asyncio
    async def test_unrelated_references_untouched(self, index, file_refs, tmp_path):
        """Deleting one item must not touch references that don't involve it.

        Regression guard: an over-broad cascade query (e.g. one that
        accidentally drops references in the same *commit* as a deleted
        item rather than references *involving* that item) would silently
        delete unrelated edges."""
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                *(
                    {
                        "op": "upsert_item",
                        "id": iid,
                        "type": "file",
                        "name": f"{iid}.py",
                        "path": f"{iid}.py",
                        "content": iid,
                    }
                    for iid in ("doomed", "p", "q")
                ),
                {
                    "op": "upsert_reference",
                    "from_id": "p",
                    "to_id": "q",
                    "relation": "imports",
                    "meta": {},
                },
                {"op": "delete_item", "id": "doomed"},
            ],
        )
        await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)

        # ``doomed`` is gone, ``p → q`` lives.
        assert await index.get_item("doomed") is None
        assert await file_refs.get(from_uuid="p", to_uuid="q", relation="imports") is not None

    @pytest.mark.asyncio
    async def test_delete_item_with_no_references_is_a_noop_for_refs(
        self, index, file_refs, tmp_path
    ):
        """Most deleted items have no edges (entities in a freshly-added
        and freshly-removed file). The cascade must report 0 in that case
        and not raise."""
        path = _write_jsonl(
            tmp_path / "delta.jsonl",
            [
                {"op": "commit", "sha": "s1"},
                {
                    "op": "upsert_item",
                    "id": "lonely",
                    "type": "file",
                    "name": "lonely.py",
                    "path": "lonely.py",
                    "content": "x",
                },
                {"op": "delete_item", "id": "lonely"},
            ],
        )
        stats = await apply_delta(index=index, file_refs=file_refs, jsonl_path=path)

        assert stats.items_deleted == 1
        assert stats.references_deleted == 0
