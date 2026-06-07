"""SQLAlchemy ORM models for ``/loop`` persistence.

Two tables live in the project's ``state.db`` (same file the
scheduler uses):

* ``loop_state`` — single-row store of the active loop. The
  primary key is hard-pinned to ``1`` via a CHECK constraint so
  upserts can target a known id instead of inventing one.
* ``loop_progress`` — per-(run_id, key) key/value rows. Scoped by
  ``run_id`` so progress from an old loop run doesn't leak into a
  fresh one; rows are kept after the loop ends so the audit trail
  survives (cleared explicitly via
  :py:meth:`LoopProgressStore.clear` if the user wants).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base


class LoopStateModel(Base):
    __tablename__ = "loop_state"
    # Single-row enforcement — the active ``/loop`` is a singleton
    # per project, so every write targets ``id=1``. A CHECK
    # constraint guards against a buggy caller inserting a second
    # row.
    __table_args__ = (CheckConstraint("id = 1", name="ck_loop_state_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    iteration_index: Mapped[int] = mapped_column(Integer, nullable=False)
    iterations_remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    # See :class:`LoopState` for the explicit-vs-implicit cap
    # semantic. Persisted so a restart resumes with the right
    # cap-hit behavior (terminate vs. auto-extend).
    cap_explicit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class LoopProgressModel(Base):
    __tablename__ = "loop_progress"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_loop_progress_run_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
