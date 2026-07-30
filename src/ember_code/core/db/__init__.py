"""Shared database layer — SQLAlchemy + alembic, SQLite-backed.

This module is the single import point for the DB stack:

- :class:`Base` from :mod:`ember_code.core.db.base` is the declarative base
  every ORM model registers against.
- :class:`ember_code.core.db.engine_registry.EngineRegistry` is the
  canonical entry point for engine + sessionmaker access. The
  :class:`ember_code.core.db.engine.EngineCache` coordinator composes
  a shared :class:`EngineRegistry` instance and exposes the historical
  call surface (:func:`get_engine`, :func:`get_sessionmaker`,
  :func:`get_async_engine`, :func:`get_async_sessionmaker`,
  :func:`dispose_all`) as bound methods on its module-level singleton
  ``ember_code.core.db.engine.cache``. Legacy imports keep working;
  new callers should construct their own ``EngineCache`` for isolation.
- URL helpers (:func:`sync_url`, :func:`async_url`) live in
  :mod:`ember_code.core.db.urls` and are re-exported from
  :mod:`ember_code.core.db.engine` for back-compat.
- Submodule imports (e.g. ``ember_code.core.code_index.db.models``) attach
  their tables to ``Base.metadata`` — alembic ``--autogenerate`` and the
  programmatic upgrade in :mod:`ember_code.core.db.migrations` rely on
  those imports happening before the metadata is read.
"""
