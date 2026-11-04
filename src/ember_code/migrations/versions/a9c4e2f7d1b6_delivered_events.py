"""the service events a producer was handed, so a repeat is acknowledged and dropped

Revision ID: a9c4e2f7d1b6
Revises: d6e1f3a8b2c4
Create Date: 2026-10-26 00:00:00.000000

The platform hands a producer its services' events at least once
(igni server's service-identity §15); igni keeps each id it delivered,
scoped like every other row, so one handed out again is not delivered
twice — across restarts too.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c4e2f7d1b6"
down_revision: str | Sequence[str] | None = "d6e1f3a8b2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivered_events",
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("extension_id", sa.String(), nullable=False),
        sa.Column("delivered_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "event_id", name="pk_delivered_events"
        ),
    )


def downgrade() -> None:
    op.drop_table("delivered_events")
