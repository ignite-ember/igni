"""Tests for the SQLite layer of code_index (SQLAlchemy ORM + alembic).

Each test gets its own tmp file — file isolation gives us project
scoping without a tenant column.
"""

from __future__ import annotations

import pytest

from ember_code.core.code_index.pg.commit_metadata import CommitMetadataService
from ember_code.core.code_index.pg.file_reference import FileReferenceService
from ember_code.core.db.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "state.db")


@pytest.fixture
def file_refs(db):
    return FileReferenceService(db)


@pytest.fixture
def commits(db):
    return CommitMetadataService(db)


# -- file_reference -----------------------------------------------------------


async def test_create_get_exists(file_refs):
    await file_refs.create(from_uuid="a", to_uuid="b", relation="imports", meta={"line": 5})
    assert await file_refs.exists(from_uuid="a", to_uuid="b", relation="imports")
    assert not await file_refs.exists(from_uuid="a", to_uuid="missing", relation="imports")
    ref = await file_refs.get(from_uuid="a", to_uuid="b", relation="imports")
    assert ref is not None
    assert ref.relation == "imports"
    assert ref.meta == {"line": 5}


async def test_create_is_upsert(file_refs):
    await file_refs.create(from_uuid="a", to_uuid="b", relation="calls", meta={})
    await file_refs.create(from_uuid="a", to_uuid="b", relation="calls", meta={"x": 1})
    ref = await file_refs.get(from_uuid="a", to_uuid="b", relation="calls")
    assert ref is not None
    assert ref.meta == {"x": 1}


async def test_pair_can_carry_multiple_relations(file_refs):
    """A→B can be both ``imports`` and ``calls`` simultaneously — relation is part of the key."""
    await file_refs.create(from_uuid="a", to_uuid="b", relation="imports", meta={})
    await file_refs.create(from_uuid="a", to_uuid="b", relation="calls", meta={})
    refs = await file_refs.get_by_uuids(uuids=["a"])
    assert {r.relation for r in refs} == {"imports", "calls"}


async def test_get_by_uuids(file_refs):
    await file_refs.create(from_uuid="a", to_uuid="b", relation="imports", meta={})
    await file_refs.create(from_uuid="b", to_uuid="c", relation="calls", meta={})
    await file_refs.create(from_uuid="x", to_uuid="y", relation="imports", meta={})
    refs = await file_refs.get_by_uuids(uuids=["b"])
    pairs = {(r.from_uuid, r.to_uuid) for r in refs}
    assert pairs == {("a", "b"), ("b", "c")}


async def test_get_by_uuids_filters_relations(file_refs):
    await file_refs.create(from_uuid="a", to_uuid="b", relation="imports", meta={})
    await file_refs.create(from_uuid="b", to_uuid="a", relation="imported_by", meta={})
    await file_refs.create(from_uuid="b", to_uuid="c", relation="calls", meta={})

    only_calls = await file_refs.get_by_uuids(uuids=["a", "b", "c"], relations=["calls"])
    assert {r.relation for r in only_calls} == {"calls"}

    import_pair = await file_refs.get_by_uuids(uuids=["a"], relations=["imports", "imported_by"])
    assert {(r.from_uuid, r.to_uuid, r.relation) for r in import_pair} == {
        ("a", "b", "imports"),
        ("b", "a", "imported_by"),
    }


async def test_query_by_relation(file_refs):
    await file_refs.create(from_uuid="a", to_uuid="b", relation="imports", meta={})
    await file_refs.create(from_uuid="c", to_uuid="d", relation="imports", meta={})
    await file_refs.create(from_uuid="b", to_uuid="c", relation="calls", meta={})

    imports = await file_refs.query_by_relation("imports")
    assert {(r.from_uuid, r.to_uuid) for r in imports} == {("a", "b"), ("c", "d")}


async def test_delete_one_relation_keeps_others(file_refs):
    await file_refs.create(from_uuid="a", to_uuid="b", relation="imports", meta={})
    await file_refs.create(from_uuid="a", to_uuid="b", relation="calls", meta={})

    await file_refs.delete(from_uuid="a", to_uuid="b", relation="imports")
    surviving = await file_refs.get_by_uuids(uuids=["a"])
    assert [r.relation for r in surviving] == ["calls"]


async def test_delete_pair_drops_all_relations(file_refs):
    await file_refs.create(from_uuid="a", to_uuid="b", relation="imports", meta={})
    await file_refs.create(from_uuid="a", to_uuid="b", relation="calls", meta={})

    await file_refs.delete(from_uuid="a", to_uuid="b")
    assert await file_refs.get_by_uuids(uuids=["a"]) == []


async def test_delete_by_uuid_drops_all_directions(file_refs):
    await file_refs.create(from_uuid="a", to_uuid="b", relation="imports", meta={})
    await file_refs.create(from_uuid="b", to_uuid="c", relation="calls", meta={})
    await file_refs.create(from_uuid="x", to_uuid="y", relation="imports", meta={})

    removed = await file_refs.delete_by_uuid(uuid="b")
    assert removed == 2
    assert await file_refs.get_by_uuids(uuids=["b"]) == []
    assert len(await file_refs.get_by_uuids(uuids=["x", "y"])) == 1


async def test_project_isolation_via_separate_files(tmp_path):
    """Two ``Database`` instances on different files share nothing."""
    db_a = Database(tmp_path / "proj_a.db")
    db_b = Database(tmp_path / "proj_b.db")
    refs_a = FileReferenceService(db_a)
    refs_b = FileReferenceService(db_b)

    await refs_a.create(from_uuid="a", to_uuid="b", relation="imports", meta={"side": "a"})
    await refs_b.create(from_uuid="a", to_uuid="b", relation="imports", meta={"side": "b"})
    assert (await refs_a.get(from_uuid="a", to_uuid="b", relation="imports")).meta == {"side": "a"}
    assert (await refs_b.get(from_uuid="a", to_uuid="b", relation="imports")).meta == {"side": "b"}


# -- commit_metadata ----------------------------------------------------------


async def test_create_or_update_and_fetch(commits):
    await commits.create_or_update(
        item_id="i1",
        commit_sha="sha1",
        key="line_range",
        value={"line_from": 1, "line_to": 30},
    )
    found = await commits.get_by_items_and_commit(
        item_ids=["i1"], commit_sha="sha1", key="line_range"
    )
    assert found == {"i1": {"line_from": 1, "line_to": 30}}


async def test_create_or_update_is_upsert(commits):
    await commits.create_or_update(
        item_id="i1", commit_sha="sha1", key="line_range", value={"v": 1}
    )
    await commits.create_or_update(
        item_id="i1", commit_sha="sha1", key="line_range", value={"v": 2}
    )
    found = await commits.get_by_items_and_commit(
        item_ids=["i1"], commit_sha="sha1", key="line_range"
    )
    assert found == {"i1": {"v": 2}}


async def test_bulk_create_or_update(commits):
    await commits.bulk_create_or_update(
        commit_sha="sha1",
        key="line_range",
        items=[
            {"item_id": "a", "value": {"line_from": 1, "line_to": 10}},
            {"item_id": "b", "value": {"line_from": 11, "line_to": 20}},
            {"item_id": "c", "value": {"line_from": 21, "line_to": 30}},
        ],
    )
    found = await commits.get_by_items_and_commit(
        item_ids=["a", "b", "c"], commit_sha="sha1", key="line_range"
    )
    assert set(found.keys()) == {"a", "b", "c"}
    assert found["b"] == {"line_from": 11, "line_to": 20}


async def test_delete_by_item_and_commit(commits):
    for sha in ("s1", "s2"):
        await commits.create_or_update(item_id="i1", commit_sha=sha, key="k", value={})
        await commits.create_or_update(item_id="i2", commit_sha=sha, key="k", value={})

    await commits.delete_by_item(item_id="i1")
    assert (
        await commits.get_by_items_and_commit(item_ids=["i1", "i2"], commit_sha="s1", key="k")
    ).keys() == {"i2"}

    await commits.delete_by_commit(commit_sha="s1")
    assert (
        await commits.get_by_items_and_commit(item_ids=["i1", "i2"], commit_sha="s1", key="k") == {}
    )
    assert (
        await commits.get_by_items_and_commit(item_ids=["i1", "i2"], commit_sha="s2", key="k")
    ).keys() == {"i2"}
