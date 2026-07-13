"""Shared database layer — SQLAlchemy + alembic, Postgres-backed.

This module is the single import point for the DB stack:

- :class:`Base` from :mod:`ember_code.core.db.base` is the declarative base
  every ORM model registers against.
- :func:`get_engine` / :func:`get_sessionmaker` from
  :mod:`ember_code.core.db.engine` give synchronous handles for code_index
  services.
- Submodule imports (e.g. ``ember_code.core.code_index.db.models``) attach
  their tables to ``Base.metadata`` — alembic ``--autogenerate`` and the
  programmatic upgrade in :mod:`ember_code.core.db.migrations` rely on
  those imports happening before the metadata is read.
"""
