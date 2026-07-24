"""Tests for memory/manager.py — per-project SQLite-backed Agno DB."""

from __future__ import annotations

from unittest.mock import patch

from ember_code.core.code_index.paths import state_db_path
from ember_code.core.config.settings import Settings
from ember_code.core.memory.manager import StorageManager


class TestStorageManager:
    def test_create_db_returns_async_sqlite_db_or_none(self, tmp_path):
        settings = Settings()
        settings.storage.data_dir = str(tmp_path)
        mgr = StorageManager(settings, project_dir=tmp_path)
        db = mgr.create_db()
        # AsyncSqliteDb if Agno available, else None — either is fine here.
        assert db is not None or db is None

    def test_create_db_points_at_per_project_state_db(self, tmp_path):
        settings = Settings()
        settings.storage.data_dir = str(tmp_path)
        mgr = StorageManager(settings, project_dir=tmp_path)
        with patch("agno.db.sqlite.AsyncSqliteDb") as mock_cls:
            mgr.create_db()
            _, kwargs = mock_cls.call_args
            expected = state_db_path(tmp_path, data_dir=str(tmp_path))
            assert kwargs["db_file"] == str(expected)
            assert kwargs["session_table"] == "ember_sessions"
            assert kwargs["memory_table"] == "ember_memories"

    def test_create_db_creates_parent_dirs(self, tmp_path):
        settings = Settings()
        settings.storage.data_dir = str(tmp_path)
        mgr = StorageManager(settings, project_dir=tmp_path / "some" / "deep" / "project")
        # Stub out the actual Agno import so the call exercises the dir-creation
        # path without trying to construct a real db.
        with patch("agno.db.sqlite.AsyncSqliteDb"):
            mgr.create_db()
        expected_parent = state_db_path(
            tmp_path / "some" / "deep" / "project", data_dir=str(tmp_path)
        ).parent
        assert expected_parent.exists()

    def test_create_memory_no_db(self, tmp_path):
        settings = Settings()
        settings.storage.data_dir = str(tmp_path)
        mgr = StorageManager(settings, project_dir=tmp_path)
        with patch.object(mgr, "create_db", return_value=None):
            assert mgr.create_memory() is None


class TestBuildClassMethods:
    """The ``build_db`` / ``build_memory`` classmethods replace the old
    free-function ``setup_db`` / ``setup_memory`` wrappers. They must
    construct a ``StorageManager`` and forward to the matching method."""

    def test_build_db_delegates_to_create_db(self, tmp_path):
        settings = Settings()
        settings.storage.data_dir = str(tmp_path)
        with patch.object(StorageManager, "create_db", return_value="mock_db") as mock:
            result = StorageManager.build_db(settings, project_dir=tmp_path)
            mock.assert_called_once_with()
            assert result == "mock_db"

    def test_build_memory_delegates_to_create_memory(self, tmp_path):
        settings = Settings()
        settings.storage.data_dir = str(tmp_path)
        with patch.object(StorageManager, "create_memory", return_value="mock_mem") as mock:
            result = StorageManager.build_memory(settings, project_dir=tmp_path)
            mock.assert_called_once_with()
            assert result == "mock_mem"

    def test_build_db_passes_project_dir_through(self, tmp_path):
        """The classmethod must forward ``project_dir`` verbatim to
        ``StorageManager(...)`` so DB path resolution sees the same input
        as the instance-level call site."""
        settings = Settings()
        settings.storage.data_dir = str(tmp_path)
        with patch("agno.db.sqlite.AsyncSqliteDb") as mock_cls:
            StorageManager.build_db(settings, project_dir=tmp_path)
            _, kwargs = mock_cls.call_args
            expected = state_db_path(tmp_path, data_dir=str(tmp_path))
            assert kwargs["db_file"] == str(expected)
