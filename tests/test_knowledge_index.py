"""Tests for ``KnowledgeIndex`` — chroma-backed per-project knowledge."""

from __future__ import annotations

import pytest

from ember_code.core.knowledge.index import KnowledgeIndex


@pytest.fixture
async def index(tmp_path):
    idx = KnowledgeIndex(project=tmp_path / "proj_a", data_dir=str(tmp_path / "data"))
    await idx.start()
    yield idx
    await idx.close()


class TestAdd:
    @pytest.mark.asyncio
    async def test_add_single_entry_round_trips(self, index):
        eid = await index.add(content="JWT authentication issues access tokens.", source="auth.md")
        assert isinstance(eid, str) and len(eid) == 16
        assert await index.has_entry(eid)
        assert await index.count() == 1

    @pytest.mark.asyncio
    async def test_add_long_content_chunks_are_searchable(self, index):
        long_content = (
            ("Authentication and JWT signing logic. " * 30)
            + "\n\n"
            + ("Database connection pool with retries. " * 30)
        )
        eid = await index.add(content=long_content, source="long.md")
        # Each topic should retrieve the same parent entry.
        for query in ("JWT signing", "database pool retries"):
            results = await index.search(query=query, limit=3)
            assert any(r["entry_id"] == eid for r in results), query

    @pytest.mark.asyncio
    async def test_re_add_same_entry_id_replaces(self, index):
        first = await index.add(content="version 1", source="v.md", entry_id="stable-id-aaaa")
        second = await index.add(
            content="version 2 totally rewritten", source="v.md", entry_id="stable-id-aaaa"
        )
        assert first == second == "stable-id-aaaa"
        assert await index.count() == 1
        results = await index.search(query="totally rewritten", limit=3)
        assert any(r["entry_id"] == "stable-id-aaaa" for r in results)

    @pytest.mark.asyncio
    async def test_add_document_uses_provided_chunks(self, index):
        eid = await index.add_document(
            chunks=["chunk one about cats", "chunk two about dogs"],
            full_content="cats and dogs",
            source="pets.md",
        )
        assert await index.has_entry(eid)
        results = await index.search(query="dogs", limit=2)
        assert any(r["entry_id"] == eid for r in results)


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_relevant_first(self, index):
        await index.add(content="Authentication and JWT token validation logic.", source="auth.md")
        await index.add(
            content="Pasta sauce simmered with garlic and olive oil.", source="recipe.md"
        )
        results = await index.search(query="JWT auth tokens", limit=2)
        assert results
        assert results[0]["source"] == "auth.md"

    @pytest.mark.asyncio
    async def test_search_truncates_long_chunk_preview(self, index):
        big = "x" * 2000
        await index.add(content=big, source="big.md")
        results = await index.search(query="x", limit=1)
        assert results
        assert len(results[0]["content"]) <= 1003  # 1000 chars + "..."

    @pytest.mark.asyncio
    async def test_cross_project_off_by_default(self, tmp_path):
        # Two siblings sharing a data root.
        a = KnowledgeIndex(project=tmp_path / "proj_a", data_dir=str(tmp_path / "data"))
        b = KnowledgeIndex(project=tmp_path / "proj_b", data_dir=str(tmp_path / "data"))
        await a.start()
        await b.start()
        try:
            await a.add(content="alpha-only secret about elephants", source="a.md")
            await b.add(content="beta-only secret about dolphins", source="b.md")

            # Default search only sees the current project's data — A's
            # elephant entry must not surface.
            local = await b.search(query="elephants", limit=3)
            assert all("elephant" not in r["parent_content"].lower() for r in local)
            assert all(r["source"] != "a.md" for r in local)
        finally:
            await a.close()
            await b.close()

    @pytest.mark.asyncio
    async def test_cross_project_on_finds_other_projects(self, tmp_path):
        a = KnowledgeIndex(project=tmp_path / "proj_a", data_dir=str(tmp_path / "data"))
        b = KnowledgeIndex(project=tmp_path / "proj_b", data_dir=str(tmp_path / "data"))
        await a.start()
        await b.start()
        try:
            await a.add(content="alpha-only secret about elephants", source="a.md")
            await b.add(content="beta-only secret about dolphins", source="b.md")

            shared = await b.search(query="elephants", limit=3, cross_project=True)
            assert any("elephant" in r["parent_content"].lower() for r in shared), shared
        finally:
            await a.close()
            await b.close()


class TestListEntries:
    @pytest.mark.asyncio
    async def test_returns_added_docs(self, index):
        await index.add(content="entry one", source="one.md", metadata={"author": "alice"})
        await index.add(content="entry two", source="two.md")

        entries = await index.list_entries()
        ids = {e["id"] for e in entries}
        assert len(ids) == 2
        author_meta = [e["metadata"].get("author") for e in entries if e["source"] == "one.md"]
        assert author_meta == ["alice"]


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_by_query_drops_doc_and_chunks(self, index):
        eid = await index.add(content="pluto is a planet", source="space.md")
        await index.add(content="mars has two moons", source="space.md")
        before = await index.count()
        deleted = await index.delete_by_query("pluto", limit=5)
        assert deleted >= 1
        assert await index.count() == before - deleted
        assert not await index.has_entry(eid)


class TestHasEntry:
    @pytest.mark.asyncio
    async def test_false_for_missing(self, index):
        assert not await index.has_entry("not-there")

    @pytest.mark.asyncio
    async def test_true_after_add(self, index):
        eid = await index.add(content="anything", source="a.md")
        assert await index.has_entry(eid)
