"""the finished calls of a turn whose spawn waits on a person

Revision ID: c4d7a2e9f5b1
Revises: b8e2f4a6d1c3
Create Date: 2026-10-26 00:00:00.000000

A turn that called ``spawn_agent`` beside other tools is continued after
a restart with every call's result: ``turn_results`` keeps those that
finished while the spawn waited, by run and tool call id.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d7a2e9f5b1"
down_revision: str | Sequence[str] | None = "b8e2f4a6d1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turn_results",
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("tool_call_id", sa.String(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "run_id", "tool_call_id", name="pk_turn_results"
        ),
    )


def downgrade() -> None:
    op.drop_table("turn_results")
