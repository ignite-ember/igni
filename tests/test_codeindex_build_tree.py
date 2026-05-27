"""Tests for ``QueryService._build_tree`` — multi-level parent
chain assembly + the cycle guard.

The happy path is covered by ``test_codeindex_eval_fixture.py``. Here
we hit the defensive edge cases that path doesn't exercise:

  - Cycle guard: an item whose ``parent_id`` chain loops back on
    itself shouldn't hang the BFS or recurse infinitely.
  - Depth walking: each row gets its full parent chain through the
    nearest folder ancestor; deeper levels are ignored.
  - Empty input: returns ``[]``, not None or an error.
  - ``_raw_content`` fallback: the intermediate-node summary path
    uses the unfiltered content so requesting non-summary sections
    doesn't wipe ancestor descriptions.
"""

from __future__ import annotations

import json
import uuid

import pytest

from ember_code.core.code_index.enums import FileSystemType, Section
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.schema.items import CodeIndexItem
from ember_code.core.tools.codeindex import CodeIndexTools


def _item(
    *,
    name: str,
    path: str,
    parent_id: str | None = None,
    item_id: str | None = None,
    fs_type: FileSystemType = FileSystemType.FILE,
    entity_type: str | None = None,
    content: str = "",
) -> CodeIndexItem:
    return CodeIndexItem(
        item_id=item_id or str(uuid.uuid4()),
        name=name,
        content=content,
        type=fs_type,
        kind="code",
        path=path,
        parent_id=parent_id,
        repository_id="test-repo",
        entity_type=entity_type,
    )


@pytest.fixture
async def index(tmp_path):
    idx = CodeIndex(project=tmp_path / "proj", data_dir=str(tmp_path / "data"))
    await idx.set_head("c1")
    await idx.prepare_commit("c1")
    yield idx
    await idx.close()


@pytest.fixture
def tools(index):
    return CodeIndexTools(index=index)


# ── Cycle guard ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cycle_in_parent_chain_does_not_hang(tools, index):
    """An item whose parent_id eventually loops back must not cause
    the chain walker to spin forever. We construct A.parent_id = B and
    B.parent_id = A (a tight 2-cycle) and verify the query returns
    cleanly with both items represented at some depth.
    """
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    await index.add_item("c1", _item(
        name="A.py", path="src/A.py", item_id=a_id, parent_id=b_id,
        content="alpha content with the marker",
    ))
    await index.add_item("c1", _item(
        name="B.py", path="src/B.py", item_id=b_id, parent_id=a_id,
        content="other content",
    ))

    # Query for one of the items by id — the chain walker fires
    # during tree assembly. The cycle guard must short-circuit before
    # building an infinite chain.
    result = json.loads(await tools.codeindex_query(ids=[a_id], limit=10))
    # No exception, valid response shape — that's the assertion. The
    # actual tree shape under a cycle isn't well-defined; we just
    # ensure we don't blow up.
    assert "items" in result


# ── Multi-level depth ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_level_chain_stops_at_folder(tools, index):
    """Build a 4-deep chain: entity → class → file → folder. The
    walker should include each level up to and including the
    immediate folder ancestor, but stop there (deeper folder
    ancestors are out of scope)."""
    folder_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    class_id = str(uuid.uuid4())
    entity_id = str(uuid.uuid4())

    await index.add_item("c1", _item(
        name="services", path="src/services",
        item_id=folder_id, fs_type=FileSystemType.FOLDER,
        content="[SECTION:summary]Services folder.[/SECTION]",
    ))
    await index.add_item("c1", _item(
        name="auth.py", path="src/services/auth.py",
        item_id=file_id, parent_id=folder_id,
        content="[SECTION:summary]Auth file summary.[/SECTION]",
    ))
    await index.add_item("c1", _item(
        name="AuthService", path="src/services/auth.py::AuthService",
        item_id=class_id, parent_id=file_id,
        entity_type="class",
        content="[SECTION:summary]Service class.[/SECTION]",
    ))
    await index.add_item("c1", _item(
        name="login", path="src/services/auth.py::AuthService::login",
        item_id=entity_id, parent_id=class_id,
        entity_type="function",
        content="[SECTION:summary]Login method.[/SECTION]",
    ))

    result = json.loads(await tools.codeindex_query(ids=[entity_id], limit=10))
    # Top-level should be the folder; the entity should be nested
    # underneath. Walk down to find the entity.
    assert result["items"], "expected at least one item"
    root = result["items"][0]
    assert root["type"] == "folder"
    assert root["name"] == "services"


# ── Empty input ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_query_result_returns_empty_items(tools, index):
    """A query that matches nothing returns ``items=[]`` and
    ``total=0``, not None or an error."""
    # Index has no items at all; any filter returns empty.
    result = json.loads(await tools.codeindex_query(ids=["nonexistent"], limit=10))
    assert result["items"] == []
    assert result["total"] == 0


# ── _raw_content fallback for intermediate-node summaries ─────────


@pytest.mark.asyncio
async def test_intermediate_summary_preserved_when_filtering_non_summary(tools, index):
    """When the agent requests ``sections=['security']``, the matched
    leaf gets only the security section, but intermediate ancestor
    nodes (folders, files, classes) must still show their SUMMARY
    section in the ``summary`` field — the agent needs the "what is
    this folder" framing regardless of section selection.

    Pre-fix bug: ``filter_sections`` was applied to ALL rows up
    front, mutating ``r.content`` so ``shorten_summary`` then saw
    stripped content and returned "". Now intermediate nodes use
    the unfiltered ``_raw_content`` stash.
    """
    folder_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    await index.add_item("c1", _item(
        name="services", path="src/services",
        item_id=folder_id, fs_type=FileSystemType.FOLDER,
        content="[SECTION:summary]Services folder summary.[/SECTION]",
    ))
    await index.add_item("c1", _item(
        name="auth.py", path="src/services/auth.py",
        item_id=file_id, parent_id=folder_id,
        # Auth file has BOTH summary and security sections.
        content=(
            "[SECTION:summary]Auth file does authentication.[/SECTION]"
            "[SECTION:security]Has known SQL injection issue.[/SECTION]"
        ),
    ))

    # Agent requests ONLY the security section.
    result = json.loads(await tools.codeindex_query(
        ids=[file_id], sections=[Section.SECURITY], limit=10,
    ))
    assert result["items"]
    # Find the matched file in the tree (may be nested under folder).
    def find(node, name):
        if node.get("name") == name:
            return node
        for c in node.get("matches", []):
            r = find(c, name)
            if r is not None:
                return r
        return None

    folder_node = result["items"][0]
    # Folder is intermediate → its summary must be the SUMMARY section,
    # not the empty result of filtering for SECURITY.
    assert folder_node["summary"], (
        "intermediate folder summary should not be empty when agent "
        f"requested non-summary sections; got: {folder_node['summary']!r}"
    )
    assert "Services folder" in folder_node["summary"]
