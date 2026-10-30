"""Helpers for the tests that run igni's stores on both databases.

SQLite always; PostgreSQL when ``IGNI_TEST_DATABASE_URL`` names a server
the tests may create and drop databases on, e.g.::

    docker run --rm -d --name igni-pg-test -e POSTGRES_PASSWORD=pw \\
        -p 55432:5432 postgres:17
    IGNI_TEST_DATABASE_URL=postgresql://postgres:pw@localhost:55432/postgres \\
        pytest tests/test_storage_backends.py -rs

The fixtures (``database_url``, ``postgres_url``) are in ``conftest.py``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from ember_code.core.db.database import Database
from ember_code.core.db.engine import cache
from ember_code.core.db.scope import StorageScope, Tenant
from ember_code.core.db.urls import DatabaseUrl


class ThrowawayPostgres:
    """A database of the run's own on the server ``IGNI_TEST_DATABASE_URL`` names."""

    def __init__(self, server_url: str) -> None:
        self._server = make_url(server_url).set(drivername="postgresql+psycopg")
        self._name = f"igni_test_{uuid.uuid4().hex[:12]}"

    def _admin(self) -> Engine:
        return create_engine(self._server, isolation_level="AUTOCOMMIT")

    def create(self) -> str:
        admin = self._admin()
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{self._name}"'))
        admin.dispose()
        return self._server.set(database=self._name).render_as_string(hide_password=False)

    def drop(self) -> None:
        cache.dispose_all()
        admin = self._admin()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{self._name}" WITH (FORCE)'))
        admin.dispose()


class Scopes:
    """Scopes nobody else in the run uses."""

    @staticmethod
    def fresh(project: str = "project") -> StorageScope:
        return StorageScope.for_tenant(
            Tenant(org=f"org-{uuid.uuid4().hex[:8]}", producer=f"person:{uuid.uuid4().hex[:8]}"),
            project=project,
        )

    @classmethod
    def pair(cls, url: DatabaseUrl) -> tuple[Database, Database]:
        """Two handles on one database, for two producers."""
        first = Database(url, scope=cls.fresh())
        return first, first.in_scope(cls.fresh())
