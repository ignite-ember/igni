"""SQLAlchemy ORM model for the scheduler tasks table.

Kept separate from ``models.py`` (Pydantic domain types) so importing
the domain models doesn't drag in SQLAlchemy. Alembic env imports this
module to register the table on ``Base.metadata``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base


class ScheduledTaskModel(Base):
    __tablename__ = "scheduler_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    result: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    recurrence: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
