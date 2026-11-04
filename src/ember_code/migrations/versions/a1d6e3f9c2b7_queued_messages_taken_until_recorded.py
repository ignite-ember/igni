"""a queued message is taken, then deleted once the conversation holds it

Revision ID: a1d6e3f9c2b7
Revises: f7c2a8e4b1d3
Create Date: 2026-10-26 00:00:00.000000

Taking a message used to delete its row, so a run that died after taking
it lost it. ``status`` keeps it (``waiting`` → ``taken``) until the
session's conversation holds it under the row's id; a backend that
starts offers every taken message the conversation does not hold again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1d6e3f9c2b7"
down_revision: str | Sequence[str] | None = "f7c2a8e4b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("queued_messages") as batch:
        batch.add_column(sa.Column("status", sa.String(), server_default="waiting", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("queued_messages") as batch:
        batch.drop_column("status")
