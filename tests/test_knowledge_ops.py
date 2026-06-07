"""Tests for ``SessionKnowledgeManager`` — wraps ``KnowledgeIndex`` for sessions."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ember_code.core.config.settings import Settings
from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.session.knowledge_ops import SessionKnowledgeManager


def _facade_double() -> AsyncMock:
    facade = AsyncMock(spec=KnowledgeIndex)
    facade.add = AsyncMock(return_value="abc1234567")
    facade.search = AsyncMock(return_value=[])
    facade.count = AsyncMock(return_value=42)
    return facade


class TestShareEnabled:
    def test_enabled_when_all_conditions_met(self, tmp_path):
        settings = Settings()
        settings.knowledge.enabled = True
        settings.knowledge.share = True
        mgr = SessionKnowledgeManager(_facade_double(), settings, tmp_path)
        assert mgr.share_enabled() is True

    def test_disabled_when_knowledge_none(self, tmp_path):
        settings = Settings()
        mgr = SessionKnowledgeManager(None, settings, tmp_path)
        assert mgr.share_enabled() is False

    def test_disabled_when_share_false(self, tmp_path):
        settings = Settings()
        settings.knowledge.share = False
        mgr = SessionKnowledgeManager(_facade_double(), settings, tmp_path)
        assert mgr.share_enabled() is False


class TestAdd:
    @pytest.mark.asyncio
    async def test_fails_when_no_knowledge(self, tmp_path):
        mgr = SessionKnowledgeManager(None, Settings(), tmp_path)
        result = await mgr.add(text="hello")
        assert not result.success

    @pytest.mark.asyncio
    async def test_text_is_required(self, tmp_path):
        mgr = SessionKnowledgeManager(_facade_double(), Settings(), tmp_path)
        with pytest.raises(TypeError):
            await mgr.add()

    @pytest.mark.asyncio
    async def test_adds_text(self, tmp_path):
        facade = _facade_double()
        settings = Settings()
        settings.knowledge.share = False
        mgr = SessionKnowledgeManager(facade, settings, tmp_path)
        result = await mgr.add(text="Some knowledge")
        assert result.success
        facade.add.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mirrors_to_yaml_when_share_enabled(self, tmp_path):
        facade = _facade_double()
        settings = Settings()
        settings.knowledge.enabled = True
        settings.knowledge.share = True
        mgr = SessionKnowledgeManager(facade, settings, tmp_path)
        await mgr.add(text="entry one", metadata={"source": "test"})
        share_file = tmp_path / settings.knowledge.share_file
        assert share_file.exists()
        assert "entry one" in share_file.read_text()


class TestSearch:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_knowledge(self, tmp_path):
        mgr = SessionKnowledgeManager(None, Settings(), tmp_path)
        result = await mgr.search("test")
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_returns_results_from_facade(self, tmp_path):
        facade = _facade_double()
        facade.search.return_value = [
            {
                "content": "result content",
                "name": "result.py",
                "score": 0.9,
                "source": "docs",
                "project": "abc",
                "metadata": {"source": "docs"},
            }
        ]
        mgr = SessionKnowledgeManager(facade, Settings(), tmp_path)
        result = await mgr.search("test", limit=5)
        assert result.total == 1
        assert result.results[0].content == "result content"
        assert result.results[0].metadata.get("project") == "abc"

    @pytest.mark.asyncio
    async def test_search_swallows_facade_errors(self, tmp_path):
        facade = _facade_double()
        facade.search.side_effect = RuntimeError("boom")
        mgr = SessionKnowledgeManager(facade, Settings(), tmp_path)
        assert (await mgr.search("anything")).total == 0


class TestStatus:
    @pytest.mark.asyncio
    async def test_disabled_when_no_knowledge(self, tmp_path):
        mgr = SessionKnowledgeManager(None, Settings(), tmp_path)
        status = await mgr.status()
        assert status.enabled is False

    @pytest.mark.asyncio
    async def test_enabled_with_knowledge(self, tmp_path):
        facade = _facade_double()
        mgr = SessionKnowledgeManager(facade, Settings(), tmp_path)
        status = await mgr.status()
        assert status.enabled is True
        assert status.document_count == 42
        assert status.embedder.startswith("sentence-transformers:")
