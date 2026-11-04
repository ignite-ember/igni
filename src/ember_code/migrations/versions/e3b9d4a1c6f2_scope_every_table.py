"""every igni table is alembic's, and carries org, producer and project

Revision ID: e3b9d4a1c6f2
Revises: c7f1a9e2d8b4
Create Date: 2026-10-26 00:00:00.000000

``docs/CLOUD.md`` section 9. igni's state moves into PostgreSQL in the
cloud, one database per org shared by its people and apps, so every
table igni owns carries the org, the producer and the project a row
belongs to, with the scope in each key that needs to be unique. On the
desktop the three columns hold ``""``: a project's ``state.db`` is
already its own.

Two kinds of table change here:

* The alembic tables — ``scheduler_tasks``, ``loop_state``,
  ``loop_progress``, ``background_processes`` — get the columns and
  scoped keys.
* The tables the raw ``sqlite3`` stores created for themselves with
  ``CREATE TABLE IF NOT EXISTS`` and ad-hoc ``ALTER``s — received
  messages, session preferences, session directories, client state,
  local notifications — become alembic's, written once, dialect-neutral.

And two are new: ``memory_files`` (``MEMORY.md`` memories in the cloud)
and ``attachments`` (uploads kept as PostgreSQL large objects).

No users yet, so no data migration: the tables are dropped and created
again, and what a development database held in them is gone.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3b9d4a1c6f2"
down_revision: str | Sequence[str] | None = "c7f1a9e2d8b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What the raw ``sqlite3`` stores made for themselves in an existing
#: desktop file, if they ever ran there.
_UNMANAGED_TABLES = (
    "ember_received_messages",
    "ember_session_preferences",
    "ember_session_directories",
    "ember_client_state",
    "local_notifications",
)


def _scope() -> list[sa.Column]:
    return [
        sa.Column("org", sa.String(), server_default="", nullable=False),
        sa.Column("producer", sa.String(), server_default="", nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
    ]


def upgrade() -> None:
    op.drop_table("scheduler_tasks")
    op.drop_index(op.f("ix_loop_progress_run_id"), table_name="loop_progress")
    op.drop_table("loop_progress")
    op.drop_table("loop_state")
    op.drop_table("background_processes")
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in _UNMANAGED_TABLES:
        if table in existing:
            op.drop_table(table)

    op.create_table(
        "scheduler_tasks",
        *_scope(),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("result", sa.Text(), server_default="", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("recurrence", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduler_tasks_scope", "scheduler_tasks", ["org", "producer", "project"])

    op.create_table(
        "loop_state",
        *_scope(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("iteration_index", sa.Integer(), nullable=False),
        sa.Column("iterations_remaining", sa.Integer(), nullable=False),
        sa.Column("cap_explicit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_loop_state_singleton"),
        sa.PrimaryKeyConstraint("org", "producer", "project", "id", name="pk_loop_state"),
    )

    op.create_table(
        "loop_progress",
        *_scope(),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org", "producer", "project", "run_id", "key", name="uq_loop_progress_run_key"
        ),
    )
    op.create_index(op.f("ix_loop_progress_run_id"), "loop_progress", ["run_id"])

    op.create_table(
        "background_processes",
        *_scope(),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("cmd", sa.Text(), nullable=False),
        sa.Column("pgid", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "pid", name="pk_background_processes"
        ),
    )

    op.create_table(
        "ember_received_messages",
        *_scope(),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("completed_at", sa.Integer(), nullable=True),
        sa.Column("interrupted_at", sa.Integer(), nullable=True),
        sa.Column("interrupted_reason", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index(
        "ix_ember_received_messages_session_status",
        "ember_received_messages",
        ["org", "producer", "project", "session_id", "status"],
    )

    op.create_table(
        "ember_session_preferences",
        *_scope(),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=True),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "session_id", name="pk_ember_session_preferences"
        ),
    )

    op.create_table(
        "ember_session_directories",
        *_scope(),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("project_dir", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "session_id", name="pk_ember_session_directories"
        ),
    )

    op.create_table(
        "ember_client_state",
        *_scope(),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "org", "producer", "project", "client_id", "key", name="pk_ember_client_state"
        ),
    )

    op.create_table(
        "local_notifications",
        *_scope(),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("event_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("read_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org", "producer", "project", "source", "event_key", name="uq_local_notifications_event"
        ),
    )
    op.create_index(
        "ix_local_notifications_scope_created",
        "local_notifications",
        ["org", "producer", "project", "created_at"],
    )

    op.create_table(
        "memory_files",
        *_scope(),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("org", "producer", "project", "name", name="pk_memory_files"),
    )

    op.create_table(
        "attachments",
        *_scope(),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("oid", sa.BigInteger(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attachments_scope_session",
        "attachments",
        ["org", "producer", "project", "session_id"],
    )


def downgrade() -> None:
    """Back to the unscoped alembic tables, empty; the stores' own
    tables are left to be created by the code of that revision."""
    op.drop_index("ix_attachments_scope_session", table_name="attachments")
    op.drop_table("attachments")
    op.drop_table("memory_files")
    op.drop_index("ix_local_notifications_scope_created", table_name="local_notifications")
    op.drop_table("local_notifications")
    op.drop_table("ember_client_state")
    op.drop_table("ember_session_directories")
    op.drop_table("ember_session_preferences")
    op.drop_index("ix_ember_received_messages_session_status", table_name="ember_received_messages")
    op.drop_table("ember_received_messages")
    op.drop_table("background_processes")
    op.drop_index(op.f("ix_loop_progress_run_id"), table_name="loop_progress")
    op.drop_table("loop_progress")
    op.drop_table("loop_state")
    op.drop_index("ix_scheduler_tasks_scope", table_name="scheduler_tasks")
    op.drop_table("scheduler_tasks")

    op.create_table(
        "scheduler_tasks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("result", sa.Text(), server_default="", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("recurrence", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "loop_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("iteration_index", sa.Integer(), nullable=False),
        sa.Column("iterations_remaining", sa.Integer(), nullable=False),
        sa.Column("cap_explicit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_loop_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "loop_progress",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "key", name="uq_loop_progress_run_key"),
    )
    op.create_index(op.f("ix_loop_progress_run_id"), "loop_progress", ["run_id"])
    op.create_table(
        "background_processes",
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("cmd", sa.Text(), nullable=False),
        sa.Column("pgid", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("pid"),
    )
