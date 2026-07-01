"""add background_processes table

Revision ID: b3a8c2e5d4f1
Revises: 8c2e7b1a4f9d
Create Date: 2026-06-30 00:00:00.000000

Tracks every backgrounded shell process the BE has spawned so
the watcher panel can surface them across BE restarts. Without
this table, ``run_shell_command(background=True)`` registers a
row in the in-memory ``_ProcessRegistry`` and that row vanishes
the moment the BE process exits — but the child kept running
(``start_new_session=True`` detaches it from our process group)
and now holds whatever port / file lock it had open with no UI
trace. The user can find it via ``lsof`` but the watcher can't.

After this migration the registry's ``add`` writes here too,
``_emit_completion`` deletes the row, and ``BackendServer.startup``
re-reads alive rows as "orphan" entries so the new BE can show
+ kill them. Dead rows (the process exited between BE lifetimes)
are pruned during the same startup pass.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3a8c2e5d4f1"
down_revision: str | Sequence[str] | None = "8c2e7b1a4f9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "background_processes",
        # OS pid is the natural primary key — guaranteed unique
        # for the lifetime of the process, and the registry's
        # in-memory dict is keyed by pid too.
        sa.Column("pid", sa.Integer(), nullable=False),
        # The full shell command string the agent passed. Used
        # to render the watcher row's label after restart when
        # the live stdout is gone.
        sa.Column("cmd", sa.Text(), nullable=False),
        # Process group id from ``os.getpgid(pid)`` at spawn —
        # the orphan kill path uses ``os.killpg(pgid, SIGTERM)``
        # to wipe child processes too (a `npm run dev` spawns a
        # node + webpack tree we want to take down together).
        sa.Column("pgid", sa.Integer(), nullable=True),
        # Epoch seconds when the process was registered. Lets the
        # watcher compute elapsed time after restart (the in-
        # memory ``started_at`` is ``time.monotonic()`` which
        # resets across BE lifetimes).
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("pid"),
    )


def downgrade() -> None:
    op.drop_table("background_processes")
