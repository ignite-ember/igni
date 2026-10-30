"""an extension's settings values, per producer, in the cloud

Revision ID: d6e1f3a8b2c4
Revises: c4d7a2e9f5b1
Create Date: 2026-10-26 00:00:00.000000

The desktop keeps an extension's settings values beside its bundle; in
the cloud they are a row per extension in the shared database, scoped to
the producer like every other row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e1f3a8b2c4"
down_revision: str | Sequence[str] | None = "c4d7a2e9f5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extension_settings",
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
        sa.Column("extension_id", sa.String(), nullable=False),
        sa.Column("values", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "extension_id", name="pk_extension_settings"
        ),
    )


def downgrade() -> None:
    op.drop_table("extension_settings")
