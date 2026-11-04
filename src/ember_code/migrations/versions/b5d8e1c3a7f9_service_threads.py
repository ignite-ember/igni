"""which chat a service's thread is, and posts held for a chat whose run is going

Revision ID: b5d8e1c3a7f9
Revises: a9c4e2f7d1b6
Create Date: 2026-10-30 00:00:00.000000

A service names a conversation by a thread key; igni keeps which session
the key opened for which extension, scoped like every other row, so every
later item of the thread lands in the same chat (``docs/CLOUD.md`` §6.1).
An item with attachments that arrives while its chat's run is going is
held until the run ends, durably.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d8e1c3a7f9"
down_revision: str | Sequence[str] | None = "a9c4e2f7d1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_threads",
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
        sa.Column("extension_id", sa.String(), nullable=False),
        sa.Column("thread", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "extension_id", "thread", name="pk_service_threads"
        ),
    )
    op.create_table(
        "held_service_posts",
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("extension_id", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("held_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "event_id", name="pk_held_service_posts"
        ),
    )


def downgrade() -> None:
    op.drop_table("held_service_posts")
    op.drop_table("service_threads")
