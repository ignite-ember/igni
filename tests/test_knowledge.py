"""Tests for KnowledgeConfig + KnowledgeManager."""

from __future__ import annotations

from pathlib import Path

from ember_code.core.config.settings import KnowledgeConfig, Settings
from ember_code.core.knowledge.index import KnowledgeIndex
from ember_code.core.knowledge.manager import KnowledgeManager


class TestKnowledgeConfig:
    def test_defaults_enabled(self):
        cfg = KnowledgeConfig()
        assert cfg.enabled is True
        assert cfg.collection_name
        assert cfg.share is True

    def test_share_file_default(self):
        cfg = KnowledgeConfig()
        assert cfg.share_file.endswith(".yaml")


class TestKnowledgeManager:
    def test_returns_none_when_disabled(self, tmp_path):
        s = Settings()
        s.knowledge.enabled = False
        s.storage.data_dir = str(tmp_path)
        mgr = KnowledgeManager(s, project_dir=tmp_path)
        assert mgr.create_knowledge() is None

    def test_returns_index_when_enabled(self, tmp_path):
        s = Settings()
        s.knowledge.enabled = True
        s.storage.data_dir = str(tmp_path)
        mgr = KnowledgeManager(s, project_dir=tmp_path)
        result = mgr.create_knowledge()
        assert isinstance(result, KnowledgeIndex)
        # Same project → same project_id.
        again = KnowledgeManager(s, project_dir=tmp_path).create_knowledge()
        assert again.project_id == result.project_id

    def test_fallback_path_uses_cwd(self):
        s = Settings()
        s.knowledge.enabled = True
        mgr = KnowledgeManager(s)
        assert mgr._project_dir == Path.cwd()
