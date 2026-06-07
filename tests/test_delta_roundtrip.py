"""End-to-end roundtrip: hand-built JSONL → apply_delta → real CodeIndex.

This is the contract test the unit tests on either side don't cover.
Each line below mirrors what ember-server's emitter would produce; if the
producer ever drifts from the contract, this test catches it on first
replay.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ember_code.core.code_index import CodeIndex

COMMIT_OLD = "a" * 40
COMMIT_NEW = "b" * 40

FOLDER_ID = "folder-uuid-0001"
FILE_ID = "file-uuid-0001"
ENTITY_ID = "entity-uuid-0001"
CALLEE_FILE_ID = "file-uuid-0002"


def _write_jsonl(tmp_path: Path, lines: list[dict]) -> Path:
    target = tmp_path / "changeset.jsonl"
    with target.open("w") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return target


def _commit_op(sha: str, parent: str | None = None) -> dict:
    return {
        "op": "commit",
        "sha": sha,
        "parent_sha": parent,
        "branches": ["main"],
        "indexed_at": "2026-04-28T00:00:00+00:00",
    }


def _full_changeset() -> list[dict]:
    """A first-commit JSONL exercising every op a producer emits."""
    return [
        _commit_op(COMMIT_OLD),
        {
            "op": "upsert_item",
            "id": FOLDER_ID,
            "type": "folder",
            "name": "src",
            "path": "src",
            "content": "Source root",
            "kind": "code",
        },
        {
            "op": "upsert_item",
            "id": FILE_ID,
            "type": "file",
            "name": "auth.py",
            "path": "src/auth.py",
            "parent_id": FOLDER_ID,
            "content": "[SECTION:summary]\nHandles authentication and login.\n[/SECTION]",
            "kind": "code",
            "file_extension": ".py",
            "quality": "good",
            "security": "minor-issues",
            "domain": ["auth"],
        },
        {
            "op": "upsert_item",
            "id": CALLEE_FILE_ID,
            "type": "file",
            "name": "helpers.py",
            "path": "src/helpers.py",
            "parent_id": FOLDER_ID,
            "content": "[SECTION:summary]\nUtility helpers.\n[/SECTION]",
            "kind": "code",
            "file_extension": ".py",
        },
        {
            "op": "upsert_item",
            "id": ENTITY_ID,
            "type": "entity",
            "name": "login",
            "path": "src/auth.py::login",
            "parent_id": FILE_ID,
            "content": "[SECTION:summary]\nLogs a user in via OAuth.\n[/SECTION]",
            "kind": "code",
            "entity_type": "function",
            "file_extension": ".py",
            "line_from": 10,
            "line_to": 42,
        },
        {
            "op": "upsert_reference",
            "from_id": FILE_ID,
            "to_id": CALLEE_FILE_ID,
            "relation": "imports",
            "meta": {"source_file": "src/auth.py", "target_file": "src/helpers.py"},
        },
        {
            "op": "upsert_reference",
            "from_id": CALLEE_FILE_ID,
            "to_id": FILE_ID,
            "relation": "imported_by",
            "meta": {"importer_file": "src/auth.py", "imported_file": "src/helpers.py"},
        },
    ]


@pytest.mark.asyncio
async def test_apply_delta_indexes_items_for_search(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_dir = tmp_path / "ember"

    index = CodeIndex(project=project_dir, data_dir=data_dir)
    jsonl = _write_jsonl(tmp_path, _full_changeset())

    stats = await index.apply_delta(jsonl)

    assert stats.items_upserted == 4  # folder + 2 files + entity
    assert stats.references_upserted == 2

    # Head moved to the new commit.
    assert index.head() == COMMIT_OLD

    # Items are searchable by their summary content.
    auth_results = await index.search(query="authentication and login")
    auth_ids = [r.item_id for r in auth_results]
    assert FILE_ID in auth_ids, f"expected FILE_ID in search results, got: {auth_ids}"

    entity_results = await index.search(query="logs a user in via OAuth")
    assert ENTITY_ID in [r.item_id for r in entity_results]

    # Quality fields propagated end-to-end.
    file_row = next(r for r in auth_results if r.item_id == FILE_ID)
    assert file_row.quality == "good"
    assert file_row.security == "minor-issues"
    assert file_row.domain == ["auth"]


@pytest.mark.asyncio
async def test_apply_delta_persists_references(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_dir = tmp_path / "ember"

    index = CodeIndex(project=project_dir, data_dir=data_dir)
    jsonl = _write_jsonl(tmp_path, _full_changeset())
    await index.apply_delta(jsonl)

    file_refs = index._file_reference_service()
    forward = await file_refs.get(from_uuid=FILE_ID, to_uuid=CALLEE_FILE_ID, relation="imports")
    reverse = await file_refs.get(from_uuid=CALLEE_FILE_ID, to_uuid=FILE_ID, relation="imported_by")

    assert forward is not None and forward.relation == "imports"
    assert reverse is not None and reverse.relation == "imported_by"


@pytest.mark.asyncio
async def test_apply_incremental_delta_after_full(tmp_path):
    """Second commit copy-on-writes from parent; an upsert+delete pair lands on top."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_dir = tmp_path / "ember"

    index = CodeIndex(project=project_dir, data_dir=data_dir)

    # Commit 1: full baseline.
    await index.apply_delta(_write_jsonl(tmp_path, _full_changeset()))

    # Commit 2: add a new entity, delete the old file's import reference,
    # delete the helper file.
    new_entity_id = "entity-uuid-0002"
    incremental = [
        _commit_op(COMMIT_NEW, parent=COMMIT_OLD),
        {
            "op": "upsert_item",
            "id": new_entity_id,
            "type": "entity",
            "name": "logout",
            "path": "src/auth.py::logout",
            "parent_id": FILE_ID,
            "content": "[SECTION:summary]\nClears the session and logs the user out.\n[/SECTION]",
            "kind": "code",
            "entity_type": "function",
            "file_extension": ".py",
            "line_from": 50,
            "line_to": 80,
        },
        {"op": "delete_item", "id": CALLEE_FILE_ID},
        {"op": "delete_reference", "from_id": FILE_ID, "to_id": CALLEE_FILE_ID},
        {"op": "delete_reference", "from_id": CALLEE_FILE_ID, "to_id": FILE_ID},
    ]
    inc_jsonl = tmp_path / "inc.jsonl"
    with inc_jsonl.open("w") as fh:
        for line in incremental:
            fh.write(json.dumps(line) + "\n")

    stats = await index.apply_delta(inc_jsonl)
    assert stats.items_upserted == 1
    assert stats.items_deleted == 1
    # 2 explicit ``delete_reference`` ops + 2 cascaded by the
    # ``delete_item`` (both edges involving CALLEE_FILE_ID are
    # wiped when the item itself is deleted, per the cascade
    # introduced in 0f34104).
    assert stats.references_deleted == 4

    # Head advanced.
    assert index.head() == COMMIT_NEW

    # New entity is searchable on the new commit.
    results = await index.search(query="clears the session")
    assert new_entity_id in [r.item_id for r in results]

    # Old commit's index is untouched (copy-on-write means the new commit
    # is independent — the parent's tree didn't have these mutations).
    parent_results = await index.search(query="clears the session", commit=COMMIT_OLD)
    assert new_entity_id not in [r.item_id for r in parent_results]

    # References for the deleted helper are gone.
    file_refs = index._file_reference_service()
    assert (
        await file_refs.get(from_uuid=FILE_ID, to_uuid=CALLEE_FILE_ID, relation="imports") is None
    )
    assert (
        await file_refs.get(from_uuid=CALLEE_FILE_ID, to_uuid=FILE_ID, relation="imported_by")
        is None
    )


@pytest.mark.asyncio
async def test_replaying_same_delta_is_idempotent(tmp_path):
    """Apply the same JSONL twice — the second run shouldn't double-count or fail."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_dir = tmp_path / "ember"

    index = CodeIndex(project=project_dir, data_dir=data_dir)
    jsonl = _write_jsonl(tmp_path, _full_changeset())

    first = await index.apply_delta(jsonl)
    second = await index.apply_delta(jsonl)

    # Counts of ops applied are the same.
    assert first.items_upserted == second.items_upserted
    assert first.references_upserted == second.references_upserted

    # Search still finds the file once (not duplicated).
    results = await index.search(query="authentication and login")
    file_hits = [r for r in results if r.item_id == FILE_ID]
    assert len(file_hits) == 1
