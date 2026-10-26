"""a paused sub-agent run, and the calls waiting above it

Revision ID: b8e2f4a6d1c3
Revises: a1d6e3f9c2b7
Create Date: 2026-10-26 00:00:00.000000

A sub-agent's approval used to come back after a restart with nothing
to continue: the ``spawn_agent`` call waiting for the sub-agent died
with the backend. ``waiting_spawns`` keeps, per paused sub-agent run,
which agent it is, its own Agno session, and the chain of calls waiting
above it — the run, the tool call id and that run's messages — so a
backend that starts cold continues the sub-agent, hands its answer to
the waiting call and continues the run that made it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e2f4a6d1c3"
down_revision: str | Sequence[str] | None = "a1d6e3f9c2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waiting_spawns",
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("agent_session_id", sa.String(), nullable=False),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column("parents", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("org", "producer", "project", "run_id", name="pk_waiting_spawns"),
    )


def downgrade() -> None:
    op.drop_table("waiting_spawns")
