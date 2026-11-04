"""approvals waiting on the person and messages typed during a run

Revision ID: f7c2a8e4b1d3
Revises: e3b9d4a1c6f2
Create Date: 2026-10-26 00:00:00.000000

Both lived only in the backend's memory, so a restart lost them: an
approval dialog nobody could answer any more, a follow-up message never
delivered. In the cloud a producer's VM can be lost at any time
(``docs/CLOUD.md`` section 9). Two tables, scoped like the rest:

* ``waiting_approvals`` — one row per approval from the moment it is
  asked until it is answered or its run ends: the Agno requirement as
  Agno serialises it, and the request the window was shown;
* ``queued_messages`` — one row per message from the moment it is
  queued until a run takes it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c2a8e4b1d3"
down_revision: str | Sequence[str] | None = "e3b9d4a1c6f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _scope() -> list[sa.Column]:
    return [
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "waiting_approvals",
        *_scope(),
        sa.Column("requirement_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), server_default="", nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("request", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "requirement_id", name="pk_waiting_approvals"
        ),
    )
    op.create_index(
        "ix_waiting_approvals_session",
        "waiting_approvals",
        ["org", "producer", "project", "session_id"],
    )
    op.create_table(
        "queued_messages",
        *_scope(),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_queued_messages_session",
        "queued_messages",
        ["org", "producer", "project", "session_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_queued_messages_session", table_name="queued_messages")
    op.drop_table("queued_messages")
    op.drop_index("ix_waiting_approvals_session", table_name="waiting_approvals")
    op.drop_table("waiting_approvals")
