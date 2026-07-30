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
from typing import TYPE_CHECKING

from ember_code.core.code_index.paths import state_db_path
from ember_code.core.config.settings import Settings

if TYPE_CHECKING:
    from agno.db.base import AsyncBaseDb
    from agno.memory import MemoryManager

# Module-level imports with attribute-lookup at call sites — documented
# Rule 2 pattern used elsewhere in the codebase (e.g. plugins/loader.py,
# backend/plugin_controller.py) to preserve test patches at the source
# module (``agno.db.sqlite.AsyncSqliteDb`` / ``agno.memory.MemoryManager``)
# without a module-top ``from … import`` binding the symbol at import
# time. Rebinding at call time is what makes ``patch("agno.db.sqlite.
# AsyncSqliteDb")`` still intercept.
from agno import memory as _agno_memory
from agno.db import sqlite as _agno_sqlite

logger = logging.getLogger(__name__)


class StorageManager:
    """Factory for Agno-native database and memory backends.

    Single owner of the ``settings + project_dir`` pair: callers who only
    need a one-shot DB can use :meth:`build_db`; callers that need to
    hold the manager (e.g. so dependency injection is explicit) construct
    it directly and call :meth:`create_db` / :meth:`create_memory`.

    ``create_db()`` returns an Agno ``AsyncBaseDb`` instance suitable for
    ``Agent(db=...)``. Async so the Textual TUI and agent execution stay
    non-blocking.
    """

    def __init__(self, settings: Settings, project_dir: str | Path | None = None):
        self.settings = settings
        self.project_dir = Path(str(project_dir)) if project_dir is not None else Path.cwd()

    @classmethod
    def build_db(cls, settings: Settings, project_dir: str | Path | None = None) -> AsyncBaseDb:
        """Construct a manager and return a freshly created Agno ``AsyncBaseDb``.

        Convenience entry point for the single-call pattern where the
        caller does not need to keep the manager instance around
        (e.g. CLI resume lookup). Prefer ``StorageManager(...).create_db()``
        when the manager itself is part of the dependency graph.
        """
        return cls(settings, project_dir=project_dir).create_db()

    @classmethod
    def build_memory(
        cls, settings: Settings, project_dir: str | Path | None = None
    ) -> MemoryManager | None:
        """Construct a manager and return a freshly created Agno ``MemoryManager``.

        Convenience entry point mirroring :meth:`build_db`. Same advice
        applies — use ``StorageManager(...).create_memory()`` when the
        manager itself should be retained.
        """
        return cls(settings, project_dir=project_dir).create_memory()

    def create_db(self) -> AsyncBaseDb:
        """Create an Agno ``AsyncSqliteDb`` for agent session persistence."""
        path = state_db_path(self.project_dir, data_dir=self.settings.storage.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        return _agno_sqlite.AsyncSqliteDb(
            db_file=str(path),
            session_table="ember_sessions",
            memory_table="ember_memories",
        )

    def create_memory(self) -> MemoryManager | None:
        """Create a user memory backend (Agno ``MemoryManager``)."""
        db = self.create_db()
        if db is not None:
            return _agno_memory.MemoryManager(db=db)
        return None
