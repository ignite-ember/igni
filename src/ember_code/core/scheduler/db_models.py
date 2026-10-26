"""SQLAlchemy ORM model for the scheduler tasks table.

Kept separate from ``models.py`` (Pydantic domain types) so importing
the domain models doesn't drag in SQLAlchemy. Alembic env imports this
module to register the table on ``Base.metadata``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base
from ember_code.core.db.scope import ScopedColumns


class ScheduledTaskModel(ScopedColumns, Base):
    __tablename__ = "scheduler_tasks"
    __table_args__ = (Index("ix_scheduler_tasks_scope", "org", "producer", "project"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    result: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    recurrence: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
