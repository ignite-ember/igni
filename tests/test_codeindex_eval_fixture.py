"""Plumbing test for the CodeIndex eval fixture.

Walks the spec → JSONL → ``apply_delta`` → ``codeindex_query`` path with
no agent involved. If this passes, the eval YAML can lean on the same
fixture and trust the index will answer the canonical queries
correctly. If a query here returns the wrong file, the eval would
fail for the wrong reason (data, not agent behavior).

Real embedder runs — first execution may download the
``all-MiniLM-L6-v2`` model to ``~/.cache/chroma``. Subsequent runs hit
the cache and finish in ~2s.
"""

from __future__ import annotations

import json

import pytest

from ember_code.core.code_index.enums import (
    QualityLevel,
    Relation,
    SecurityLevel,
)
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.tools.codeindex import CodeIndexTools
from evals.codeindex.build_jsonl import build_and_write
from evals.codeindex.spec import fixture

COMMIT = "f" * 40


def _flatten(items, *, leaves_only: bool = True):
    """Walk the nested ``codeindex_query`` response tree.

    The tool returns folder→file→class→entity nesting. By default we
    yield only the *leaves* (nodes with no nested ``matches``) — those
    are the actually-matched items, ignoring the structural wrappers.
    Pass ``leaves_only=False`` to also yield every ancestor.
    """
    for item in items:
        children = item.get("matches") or []
        if children:
            if not leaves_only:
                yield item
            yield from _flatten(children, leaves_only=leaves_only)
        else:
            yield item


@pytest.fixture
async def populated_index(tmp_path):
    """Build the eval JSONL and apply it to a fresh CodeIndex."""
    project = tmp_path / "proj"
    project.mkdir()
    data_dir = tmp_path / "ember"

    jsonl_path = build_and_write(
        fixture(),
        commit_sha=COMMIT,
        output_path=tmp_path / "fixture.jsonl",
    )

    idx = CodeIndex(project=project, data_dir=data_dir)
    await idx.apply_delta(jsonl_path)
    yield idx
    await idx.close()


# ── Structural sanity ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jsonl_first_op_is_commit(tmp_path):
    """Producer contract: first line must be a commit op with parent_sha=null."""
    jsonl_path = build_and_write(fixture(), commit_sha=COMMIT, output_path=tmp_path / "f.jsonl")
    first = json.loads(jsonl_path.read_text().splitlines()[0])
    assert first["op"] == "commit"
    assert first["sha"] == COMMIT
    assert first["parent_sha"] is None


@pytest.mark.asyncio
async def test_apply_delta_lands_every_item(populated_index):
    """All 8 source files + their entities + 1 doc + 6 sections ≤ row count."""
    idx = populated_index
    docs, _ = await idx._collections(COMMIT)
    page = await __import__("asyncio").to_thread(docs.get, include=[], limit=1000)
    total = len(page.get("ids") or [])
    # 9 folders + 8 files + many entities + 1 doc file + 6 sections ≥ 30
    assert total >= 30, f"too few items in chroma: {total}"


# ── Canonical queries the eval cases rely on ────────────────────────


@pytest.mark.asyncio
async def test_critical_security_finds_login_and_upload(populated_index):
    """`security="critical"` must surface every file we've flagged.

    Two are critical: ``src/auth/login.py`` (SQL injection) and
    ``src/web/upload.py`` (path traversal). Anything tagged below
    ``critical`` (secure, minor-issues, major-issues) must NOT appear.
    """
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(security=SecurityLevel.CRITICAL, type="file")
    result = json.loads(raw)
    paths = {item["path"] for item in _flatten(result["items"])}
    assert "src/auth/login.py" in paths
    assert "src/web/upload.py" in paths
    # Items with lower severity must not leak through.
    assert "src/cache/lru.py" not in paths
    assert "src/utils/strings.py" not in paths
    assert "src/web/render.py" not in paths  # major-issues, not critical


@pytest.mark.asyncio
async def test_xss_vulnerability_filter(populated_index):
    """List filter on XSS only hits the render file, not other security cases."""
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(vulnerabilities=["xss"], type="file")
    result = json.loads(raw)
    paths = {item["path"] for item in _flatten(result["items"])}
    assert paths == {"src/web/render.py"}


@pytest.mark.asyncio
async def test_singleton_pattern_finds_settings(populated_index):
    """Pattern filter routes to the singleton anti-pattern file."""
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(patterns=["singleton"], type="file")
    result = json.loads(raw)
    paths = {item["path"] for item in _flatten(result["items"])}
    assert paths == {"src/config/settings.py"}


@pytest.mark.asyncio
async def test_high_complexity_but_good_quality(populated_index):
    """High complexity AND good quality — picks Dijkstra, not parser.py."""
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(
        complexity=__import__(
            "ember_code.core.code_index.enums", fromlist=["ComplexityLevel"]
        ).ComplexityLevel.HIGH,
        quality=QualityLevel.GOOD,
        type="file",
    )
    result = json.loads(raw)
    paths = {item["path"] for item in _flatten(result["items"])}
    assert paths == {"src/algorithms/graph.py"}
    # parser.py is very-high complexity AND poor quality — rejected on both.
    assert "src/legacy/parser.py" not in paths


@pytest.mark.asyncio
async def test_sql_injection_vulnerability_filter(populated_index):
    """List-shape filter on ``vulnerabilities`` must hit auth/login.py."""
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(vulnerabilities=["sql-injection"], type="file")
    result = json.loads(raw)
    paths = {item["path"] for item in _flatten(result["items"])}
    assert "src/auth/login.py" in paths


@pytest.mark.asyncio
async def test_refactor_candidates_surface_legacy_and_db(populated_index):
    """`needs_refactoring=true` should hit the legacy parser AND the N+1 db file."""
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(needs_refactoring=True, type="file")
    result = json.loads(raw)
    paths = {item["path"] for item in _flatten(result["items"])}
    assert "src/legacy/parser.py" in paths
    assert "src/db/queries.py" in paths
    # The well-tested utility is NOT a refactor candidate.
    assert "src/cache/lru.py" not in paths


@pytest.mark.asyncio
async def test_quality_excellent_surfaces_lru(populated_index):
    """The gold-standard utility must come through on a quality filter."""
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(quality=QualityLevel.EXCELLENT, type="file")
    result = json.loads(raw)
    paths = {item["path"] for item in _flatten(result["items"])}
    assert paths == {"src/cache/lru.py"}


@pytest.mark.asyncio
async def test_callers_of_run_raw(populated_index):
    """Reference-graph traversal: who calls ``db.queries.run_raw``?

    Drill-down via ``codeindex_tree``: it returns the run_raw entity
    with all its edges grouped by relation. ``called_by`` carries the
    callers' paths (the indexer stores the symmetric pair).
    """
    tools = CodeIndexTools(index=populated_index)
    # Find the run_raw entity id first.
    raw = await tools.codeindex_query(type="entity")
    items = json.loads(raw)["items"]
    matches = [i for i in _flatten(items) if i.get("path", "").endswith("queries.py::run_raw")]
    assert matches, "run_raw entity not found in any leaf"
    run_raw_id = matches[0]["item_id"]

    raw = await tools.codeindex_tree(id=run_raw_id, relations=[Relation.CALLED_BY])
    item = json.loads(raw)["items"][0]
    callers = {t["path"] for t in item["references"]["called_by"]}
    assert "src/auth/login.py::authenticate" in callers
    assert "src/auth/login.py::is_admin" in callers
    assert "src/db/queries.py::list_users_with_orders" in callers


@pytest.mark.asyncio
async def test_docs_section_retrieval(populated_index):
    """Markdown sections land as entity rows with kind='docs'."""
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(kind="docs", entity_type="section")
    result = json.loads(raw)
    # Hierarchical sections — yield every node (incl. ancestors), then
    # keep entries that are actually section entities.
    names = {
        item["name"]
        for item in _flatten(result["items"], leaves_only=False)
        if item.get("entity_type") == "section"
    }
    # All 6 sections of AUTH.md present.
    assert {
        "Authentication",
        "Login flow",
        "SQL safety",
        "Sessions",
        "Token leakage",
        "Roles",
    } <= names


@pytest.mark.asyncio
async def test_semantic_search_finds_token_leak(populated_index):
    """End-to-end semantic search lands on the right file via real embeddings."""
    tools = CodeIndexTools(index=populated_index)
    raw = await tools.codeindex_query(
        query_text="session token leaks back to the caller in error", limit=5
    )
    result = json.loads(raw)
    paths = [item["path"] for item in _flatten(result["items"])]
    # Top hit should be auth/session.py — the file whose summary mentions
    # the token-leak. Allow some slack: it must appear in the top-3.
    assert any("session.py" in p for p in paths[:3]), f"got: {paths}"
