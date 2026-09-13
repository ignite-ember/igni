"""background_processes keeps finished rows

Revision ID: c7f1a9e2d8b4
Revises: d4e5f6a7b8c9
Create Date: 2026-09-13 00:00:00.000000

``background_processes`` was a liveness table: a row existed while a
process ran, ``_emit_completion`` deleted it, and startup pruned rows
whose pid was gone. That answers "what is running", which is the wrong
question for the watcher — a build that failed two minutes ago is
exactly what someone opens the panel to read, and it had already been
deleted by the time they looked. Restarting the BE erased the lot.

Two nullable columns turn it into a history: ``exit_code`` (how it
ended) and ``finished_at`` (when). A row with ``finished_at`` set is
history; one without is either running or was interrupted by the BE
exiting, which the rehydrator can now tell apart and record rather than
silently prune.

Nullable and additive, so an existing database upgrades without
rewriting rows: everything already in the table predates the change and
is, correctly, "no recorded ending".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7f1a9e2d8b4"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("background_processes") as batch:
        batch.add_column(sa.Column("exit_code", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("background_processes") as batch:
        batch.drop_column("finished_at")
        batch.drop_column("exit_code")
