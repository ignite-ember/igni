"""Memory and storage setup using Agno backends.

Uses Agno's ``AsyncSqliteDb`` against the per-project ``state.db`` —
sessions, memories, and learning data all live alongside the rest of
the project's relational state, so switching projects gives a clean
slate.

Splitting memories+learning to the global ``ember.db`` while keeping
sessions per-project would require two ``AsyncBaseDb`` instances and
plumbing through ``Agent``; deferring that until the rest of phase 1
is green.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ember_code.core.code_index.paths import state_db_path
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class StorageManager:
    """Factory for Agno-native database and memory backends.

    ``create_db()`` returns an Agno ``AsyncBaseDb`` instance suitable for
    ``Agent(db=...)``. Async so the Textual TUI and agent execution stay
    non-blocking.
    """

    def __init__(self, settings: Settings, project_dir: str | Path | None = None):
        self.settings = settings
        self.project_dir = Path(str(project_dir)) if project_dir is not None else Path.cwd()

    def create_db(self) -> Any | None:
        """Create an Agno ``AsyncSqliteDb`` for agent session persistence."""
        try:
            from agno.db.sqlite import AsyncSqliteDb

            path = state_db_path(self.project_dir, data_dir=self.settings.storage.data_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            return AsyncSqliteDb(
                db_file=str(path),
                session_table="ember_sessions",
                memory_table="ember_memories",
            )
        except ImportError:
            logger.debug("agno.db.sqlite.AsyncSqliteDb not available")
            return None

    def create_memory(self) -> Any | None:
        """Create a user memory backend (Agno ``MemoryManager``)."""
        try:
            from agno.memory import MemoryManager

            db = self.create_db()
            if db is not None:
                return MemoryManager(db=db)
        except ImportError:
            logger.debug("agno.memory.MemoryManager not available")
        return None


def setup_db(settings: Settings, project_dir: str | Path | None = None) -> Any | None:
    """Create an Agno BaseDb for ``Agent(db=...)``."""
    return StorageManager(settings, project_dir=project_dir).create_db()


def setup_memory(settings: Settings, project_dir: str | Path | None = None) -> Any | None:
    """Create an Agno Memory instance."""
    return StorageManager(settings, project_dir=project_dir).create_memory()
