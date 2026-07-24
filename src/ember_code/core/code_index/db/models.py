"""SQLAlchemy ORM models for code_index relational tables.

Live in the per-project ``state.db`` (one SQLite file per project) — file
isolation gives us project scoping, no tenant column needed.

References use ``relation`` as a first-class indexed column instead of
a tag side table. One ``(from_uuid, to_uuid, relation)`` row per
edge-kind: a function that calls another function gets one
``relation="calls"`` row from the caller and one ``relation="called_by"``
row from the callee.
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, PrimaryKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.db.base import Base


class FileReferenceModel(Base):
    __tablename__ = "code_index_file_reference"

    from_uuid: Mapped[str] = mapped_column(nullable=False)
    to_uuid: Mapped[str] = mapped_column(nullable=False)
    relation: Mapped[str] = mapped_column(nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        PrimaryKeyConstraint("from_uuid", "to_uuid", "relation", name="pk_cifr"),
        Index("idx_cifr_to", "to_uuid"),
        Index("idx_cifr_relation", "relation"),
    )


class CommitMetadataModel(Base):
    __tablename__ = "code_index_commit_metadata"

    item_id: Mapped[str] = mapped_column(nullable=False)
    commit_sha: Mapped[str] = mapped_column(nullable=False)
    key: Mapped[str] = mapped_column(nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        PrimaryKeyConstraint("item_id", "commit_sha", "key", name="pk_cicm"),
        Index("idx_cicm_commit", "commit_sha", "key"),
    )
