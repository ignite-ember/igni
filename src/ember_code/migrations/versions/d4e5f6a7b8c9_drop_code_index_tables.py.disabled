"""drop code_index tables — they moved to Neo4j

Revision ID: d4e5f6a7b8c9
Revises: b3a8c2e5d4f1
Create Date: 2026-07-18 12:00:00.000000

PARKED — NOT IN THE ACTIVE ALEMBIC CHAIN.
    This file has a ``.disabled`` suffix so Alembic's ``versions/*.py``
    scan skips it. It drops ``code_index_file_reference`` and
    ``code_index_commit_metadata``, but the live SQL path
    (``code_index/index.py`` and ``delta/applier.py`` via
    ``FileReferenceService`` / ``CommitMetadataService``) still reads
    those tables — Neo4j is not wired into the indexer yet. Running
    this migration now breaks ~45 code_index tests and real indexing.

    RE-ENABLE (rename back to ``.py``) only once ``FileReferenceService``
    and ``CommitMetadataService`` route through ``Neo4jClient`` instead
    of SQLite, so the tables are genuinely unused. Until then the
    drop-tables intent is preserved here without being applied.

The code_index tables (``code_index_file_reference`` and
``code_index_commit_metadata``) moved to Neo4j in v0.10. Alembic
drops them on upgrade so the schema history reflects reality.

The cutover script (:mod:`ember_code.core.code_index.cutover`)
runs idempotent ``DROP TABLE IF EXISTS`` on the same tables at
BE startup; both code paths are safe to run together (the
second is a no-op).

Other tables in ``state.db`` (agno memory, loop, scheduler,
process store, session prefs) are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "b3a8c2e5d4f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop order matches the table FKs: child first, then parent.
    # Both tables have no FKs between them, but the indexes on
    # code_index_file_reference must come off before the table.
    op.drop_index("idx_cifr_relation", table_name="code_index_file_reference")
    op.drop_index("idx_cifr_to", table_name="code_index_file_reference")
    op.drop_table("code_index_file_reference")
    op.drop_index("idx_cicm_commit", table_name="code_index_commit_metadata")
    op.drop_table("code_index_commit_metadata")


def downgrade() -> None:
    """Recreate the SQLite tables — for rolling back the migration.

    The cutover has already moved data to Neo4j; rolling back
    leaves Neo4j authoritative. Downgrade here just restores the
    SQLite tables (empty) so existing SQLite-touching code
    doesn't error out before operators can run a re-cutover.
    """
    op.create_table(
        "code_index_commit_metadata",
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("commit_sha", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("item_id", "commit_sha", "key", name="pk_cicm"),
    )
    op.create_index(
        "idx_cicm_commit", "code_index_commit_metadata", ["commit_sha", "key"], unique=False
    )
    op.create_table(
        "code_index_file_reference",
        sa.Column("from_uuid", sa.String(), nullable=False),
        sa.Column("to_uuid", sa.String(), nullable=False),
        sa.Column("relation", sa.String(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("from_uuid", "to_uuid", "relation", name="pk_cifr"),
    )
    op.create_index("idx_cifr_to", "code_index_file_reference", ["to_uuid"], unique=False)
    op.create_index("idx_cifr_relation", "code_index_file_reference", ["relation"], unique=False)


import sqlalchemy as sa  # noqa: E402 — used by downgrade only
