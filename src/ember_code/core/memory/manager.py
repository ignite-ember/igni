"""Memory and storage setup using Agno backends.

The backend is chosen by ``storage.database_url``
(:class:`~ember_code.core.db.locator.StorageLocator`):

* unset (the desktop) — Agno's ``AsyncSqliteDb`` against the per-project
  ``state.db``: sessions, memories, and learning data live alongside the
  rest of the project's relational state, so switching projects gives a
  clean slate;
* a PostgreSQL URL (the cloud) — :class:`ScopedAsyncPostgresDb` on the
  shared database, every row kept to the session's scope through Agno's
  ``user_id`` (see that module for what this does not cover), on the
  same async engine igni's own stores use.

The one Agno db a session builds is shared by its learning machine and
its sub-agent pool, so this is the only place one is constructed.

Splitting memories+learning to the global ``ember.db`` while keeping
sessions per-project would require two ``AsyncBaseDb`` instances and
plumbing through ``Agent``; deferring that until the rest of phase 1
is green.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.config.settings import Settings
from ember_code.core.db.engine import cache
from ember_code.core.db.locator import StorageLocator
from ember_code.core.memory.scoped_postgres import ScopedAsyncPostgresDb

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

    #: Agno's table names, the same on both backends.
    SESSION_TABLE = "ember_sessions"
    MEMORY_TABLE = "ember_memories"

    def create_db(self) -> AsyncBaseDb:
        """Create the Agno db for agent session persistence: SQLite on
        the desktop, scoped PostgreSQL in the cloud."""
        storage = StorageLocator.from_settings(self.settings)
        url = storage.project_url(self.project_dir)
        path = url.file
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            return _agno_sqlite.AsyncSqliteDb(
                db_file=str(path),
                session_table=self.SESSION_TABLE,
                memory_table=self.MEMORY_TABLE,
            )
        return ScopedAsyncPostgresDb(
            scope=storage.scope(self.project_dir),
            db_engine=cache.get_async_engine(url),
            session_table=self.SESSION_TABLE,
            memory_table=self.MEMORY_TABLE,
        )

    def create_memory(self) -> MemoryManager | None:
        """Create a user memory backend (Agno ``MemoryManager``)."""
        db = self.create_db()
        if db is not None:
            return _agno_memory.MemoryManager(db=db)
        return None
