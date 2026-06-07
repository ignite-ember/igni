"""SQLAlchemy declarative base shared by every ORM model."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single declarative base for every Ember table.

    Why one ``Base``: alembic ``--autogenerate`` reads ``Base.metadata`` to
    diff against the live schema, so all owned tables must register here
    (Agno-managed tables are excluded — Agno owns its own schema).
    """
