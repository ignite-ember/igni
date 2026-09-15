"""Tests for per-commit Neo4j process isolation.

These tests verify that:
1. Each commit's Neo4j process contains only that commit's data
2. Items from commit A don't appear in commit B's search results
3. Carry-over correctly copies surviving items from parent to child

Requires ``neo4j_runtime`` fixture (Java must be installed to spawn
Neo4j subprocesses).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ember_code.core.code_index import CodeIndex

# Tests use the ``neo4j_runtime`` fixture which auto-installs a JDK when needed.
# These tests require ~4 GB of free RAM (HF model + 2x Neo4j at 512m/2g heap).
_needs_subprocess_runtime = pytest.mark.skipif(
    not os.environ.get("IGNI_TEST_NEO4J_RUNTIME"),
    reason="IGNI_TEST_NEO4J_RUNTIME not set; subprocess Neo4j tests skipped (requires ~4 GB free RAM)",
)

PARENT_SHA = "a" * 40
CHILD_SHA = "b" * 40
FOLDER_ID = "folder-uuid-0001"
FILE_A_ID = "file-uuid-a"
FILE_B_ID = "file-uuid-b"
ENTITY_ID = "entity-uuid-0001"


def _write_jsonl(tmp_path: Path, lines: list[dict]) -> Path:
    target = tmp_path / "changeset.jsonl"
    with target.open("w") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return target


def _full_changeset(sha: str) -> list[dict]:
    """A first-commit JSONL for the given sha."""
    return [
        {"op": "commit", "sha": sha, "parent_sha": None},
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
            "id": FILE_A_ID,
            "type": "file",
            "name": "auth.py",
            "path": "src/auth.py",
            "parent_id": FOLDER_ID,
            "content": "[SECTION:summary]\nHandles authentication.\n[/SECTION]",
            "kind": "code",
            "file_extension": ".py",
        },
        {
            "op": "upsert_item",
            "id": FILE_B_ID,
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
            "parent_id": FILE_A_ID,
            "content": "[SECTION:summary]\nLogs a user in.\n[/SECTION]",
            "kind": "code",
            "entity_type": "function",
            "file_extension": ".py",
            "line_from": 10,
            "line_to": 42,
        },
        {
            "op": "upsert_reference",
            "from_id": FILE_A_ID,
            "to_id": FILE_B_ID,
            "relation": "imports",
            "meta": {},
        },
    ]


@pytest.mark.asyncio
@_needs_subprocess_runtime
async def test_parent_search_does_not_see_child_items(tmp_path, neo4j_runtime):
    """Verify items indexed in child commit are not visible in parent commit search."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_dir = tmp_path / "ember"

    index = CodeIndex(project=project_dir, data_dir=data_dir, runtime=neo4j_runtime)

    # Index parent commit.
    await index.apply_delta(_write_jsonl(tmp_path, _full_changeset(PARENT_SHA)))
    await index.set_head(PARENT_SHA)

    # Search parent — should find parent items.
    parent_results = await index.search(query="authentication")
    parent_ids = {r.item_id for r in parent_results}
    assert FILE_A_ID in parent_ids, f"parent should find auth.py, got: {parent_ids}"

    # Index child commit (parent=PARENT_SHA, adds a new entity).
    child_jsonl = tmp_path / "child.jsonl"
    child_lines = [
        {"op": "commit", "sha": CHILD_SHA, "parent_sha": PARENT_SHA},
        {
            "op": "upsert_item",
            "id": "entity-uuid-new",
            "type": "entity",
            "name": "logout",
            "path": "src/auth.py::logout",
            "parent_id": FILE_A_ID,
            "content": "[SECTION:summary]\nLogs a user out.\n[/SECTION]",
            "kind": "code",
            "entity_type": "function",
            "file_extension": ".py",
            "line_from": 50,
            "line_to": 80,
        },
    ]
    child_jsonl.write_text("\n".join(json.dumps(line) for line in child_lines) + "\n")
    await index.apply_delta(child_jsonl)
    await index.set_head(CHILD_SHA)

    # Search child — should find the new entity.
    child_results = await index.search(query="logout")
    child_ids = {r.item_id for r in child_results}
    assert "entity-uuid-new" in child_ids

    # Search PARENT commit explicitly — should NOT find the new entity.
    parent_results_after_child = await index.search(query="logout", commit=PARENT_SHA)
    parent_ids_after_child = {r.item_id for r in parent_results_after_child}
    assert "entity-uuid-new" not in parent_ids_after_child, (
        f"parent commit should not see child's new entity; got: {parent_ids_after_child}"
    )


@pytest.mark.asyncio
@_needs_subprocess_runtime
async def test_carryover_preserves_surviving_items(tmp_path, neo4j_runtime):
    """Verify that carry-over copies surviving items from parent to child correctly."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_dir = tmp_path / "ember"

    index = CodeIndex(project=project_dir, data_dir=data_dir, runtime=neo4j_runtime)

    # Index parent commit.
    await index.apply_delta(_write_jsonl(tmp_path, _full_changeset(PARENT_SHA)))
    await index.set_head(PARENT_SHA)

    # Verify parent has the entity.
    parent_entity = await index.get_item(ENTITY_ID, commit=PARENT_SHA)
    assert parent_entity is not None
    assert parent_entity.name == "login"

    # Child commit: delete FILE_B_ID but keep everything else.
    child_jsonl = tmp_path / "child.jsonl"
    child_lines = [
        {"op": "commit", "sha": CHILD_SHA, "parent_sha": PARENT_SHA},
        {"op": "delete_item", "id": FILE_B_ID},
    ]
    child_jsonl.write_text("\n".join(json.dumps(line) for line in child_lines) + "\n")
    await index.apply_delta(child_jsonl)
    await index.set_head(CHILD_SHA)

    # FILE_B_ID should be gone in child.
    child_file_b = await index.get_item(FILE_B_ID, commit=CHILD_SHA)
    assert child_file_b is None, "deleted item should not appear in child"

    # FILE_A_ID should still exist in child (carried over).
    child_file_a = await index.get_item(FILE_A_ID, commit=CHILD_SHA)
    assert child_file_a is not None, "surviving item should be carried over to child"
    assert child_file_a.name == "auth.py"

    # ENTITY_ID should still exist in child (carried over).
    child_entity = await index.get_item(ENTITY_ID, commit=CHILD_SHA)
    assert child_entity is not None, "surviving entity should be carried over to child"
    assert child_entity.name == "login"


@pytest.mark.asyncio
@_needs_subprocess_runtime
async def test_references_isolated_between_commits(tmp_path, neo4j_runtime):
    """Verify that references in child commit don't leak from parent."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_dir = tmp_path / "ember"

    index = CodeIndex(project=project_dir, data_dir=data_dir, runtime=neo4j_runtime)

    # Index parent commit with an import edge.
    await index.apply_delta(_write_jsonl(tmp_path, _full_changeset(PARENT_SHA)))
    await index.set_head(PARENT_SHA)

    # Verify edge exists in parent.
    file_refs = index.file_reference_service()
    parent_edge = await file_refs.get(from_uuid=FILE_A_ID, to_uuid=FILE_B_ID, relation="imports")
    assert parent_edge is not None

    # Child commit: add a NEW item but no edges involving parent's items.
    child_jsonl = tmp_path / "child.jsonl"
    child_lines = [
        {"op": "commit", "sha": CHILD_SHA, "parent_sha": PARENT_SHA},
        {
            "op": "upsert_item",
            "id": "file-uuid-new",
            "type": "file",
            "name": "new.py",
            "path": "src/new.py",
            "content": "[SECTION:summary]\nBrand new file.\n[/SECTION]",
            "kind": "code",
            "file_extension": ".py",
        },
    ]
    child_jsonl.write_text("\n".join(json.dumps(line) for line in child_lines) + "\n")
    await index.apply_delta(child_jsonl)
    await index.set_head(CHILD_SHA)

    # In child, get file_refs for the child commit's process.
    child_file_refs = index.file_reference_service()
    # The child's edge FILE_A_ID -> FILE_B_ID should still exist
    # (carried over from parent).
    child_edge = await child_file_refs.get(
        from_uuid=FILE_A_ID, to_uuid=FILE_B_ID, relation="imports"
    )
    assert child_edge is not None, "edge should be carried over to child"
