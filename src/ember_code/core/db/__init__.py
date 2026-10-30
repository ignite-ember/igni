"""Shared database layer — SQLAlchemy + alembic, SQLite or PostgreSQL.

This module is the single import point for the DB stack:

- :class:`Base` from :mod:`ember_code.core.db.base` is the declarative base
  every ORM model registers against.
- :class:`ember_code.core.db.urls.DatabaseUrl` names a database: a SQLite
  file on the desktop, ``IGNI_DATABASE_URL`` (PostgreSQL) in the cloud.
- :class:`ember_code.core.db.engine_registry.EngineRegistry` caches engines
  and sessionmakers per database; the shared
  :class:`ember_code.core.db.engine.EngineCache` singleton
  ``ember_code.core.db.engine.cache`` is what production uses.
- :class:`ember_code.core.db.locator.StorageLocator` turns settings into
  :class:`ember_code.core.db.database.Database` handles, each with the
  :class:`ember_code.core.db.scope.StorageScope` (org, producer, project)
  every query filters by.
- :class:`ember_code.core.db.upsert.ConflictInsert` is ``INSERT … ON
  CONFLICT`` for both dialects.
- Submodule imports of model modules attach their tables to
  ``Base.metadata`` — alembic ``--autogenerate`` and the programmatic
  upgrade in :mod:`ember_code.core.db.migrations` rely on those imports
  happening before the metadata is read (``migrations/env.py``).
"""
