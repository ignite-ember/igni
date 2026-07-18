"""Tests for ``KnowledgeTools`` — agent toolkit over ``SessionKnowledgeManager``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.knowledge.models import (
    KnowledgeAddResult,
    KnowledgeDeleteResult,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    KnowledgeStatus,
)
from ember_code.core.tools.knowledge import KnowledgeTools


def _make_mgr():
    mgr = MagicMock()
    mgr.knowledge = AsyncMock()
    mgr.knowledge.delete_by_query = AsyncMock(return_value=KnowledgeDeleteResult(deleted=0))
    mgr.search = AsyncMock(return_value=KnowledgeSearchResponse(query="test"))
    mgr.add = AsyncMock(return_value=KnowledgeAddResult.ok("Added."))
    mgr.status = AsyncMock(
        return_value=KnowledgeStatus(
            enabled=True, collection_name="proj", document_count=42, embedder="ember"
        )
    )
    return mgr


class TestKnowledgeSearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        mgr = _make_mgr()
        mgr.search = AsyncMock(
            return_value=KnowledgeSearchResponse(
                query="auth",
                results=[
                    KnowledgeSearchResult(content="JWT tokens", name="auth.md"),
                    KnowledgeSearchResult(content="OAuth flow", name="oauth.md"),
                ],
                total=2,
            )
        )
        tools = KnowledgeTools(knowledge_mgr=mgr)
        result = await tools.knowledge_search("auth")
        assert "2 result" in result
        assert "JWT tokens" in result

    @pytest.mark.asyncio
    async def test_search_no_results(self):
        mgr = _make_mgr()
        tools = KnowledgeTools(knowledge_mgr=mgr)
        result = await tools.knowledge_search("nonexistent")
        assert "No knowledge found" in result


class TestKnowledgeAdd:
    @pytest.mark.asyncio
    async def test_add_success(self):
        mgr = _make_mgr()
        tools = KnowledgeTools(knowledge_mgr=mgr)
        result = await tools.knowledge_add("New pattern", source="review")
        assert "Added" in result

    @pytest.mark.asyncio
    async def test_add_failure(self):
        mgr = _make_mgr()
        mgr.add = AsyncMock(return_value=KnowledgeAddResult.fail("DB error"))
        tools = KnowledgeTools(knowledge_mgr=mgr)
        result = await tools.knowledge_add("content")
        assert "Error" in result and "DB error" in result


class TestKnowledgeDelete:
    @pytest.mark.asyncio
    async def test_delete_preview(self):
        tools = KnowledgeTools(knowledge_mgr=_make_mgr())
        result = await tools.knowledge_delete("old", confirm=False)
        assert "confirm=True" in result

    @pytest.mark.asyncio
    async def test_delete_no_knowledge(self):
        mgr = _make_mgr()
        mgr.knowledge = None
        result = await KnowledgeTools(knowledge_mgr=mgr).knowledge_delete("q", confirm=True)
        assert "not available" in result

    @pytest.mark.asyncio
    async def test_delete_dispatches_to_facade(self):
        mgr = _make_mgr()
        mgr.knowledge.delete_by_query.return_value = KnowledgeDeleteResult(deleted=3)
        result = await KnowledgeTools(knowledge_mgr=mgr).knowledge_delete("old", confirm=True)
        assert "3" in result
        mgr.knowledge.delete_by_query.assert_awaited_once_with("old")


class TestKnowledgeStatus:
    @pytest.mark.asyncio
    async def test_status_enabled(self):
        result = await KnowledgeTools(knowledge_mgr=_make_mgr()).knowledge_status()
        assert "proj" in result and "42" in result

    @pytest.mark.asyncio
    async def test_status_disabled(self):
        mgr = _make_mgr()
        mgr.status = AsyncMock(return_value=KnowledgeStatus(enabled=False))
        result = await KnowledgeTools(knowledge_mgr=mgr).knowledge_status()
        assert "disabled" in result
