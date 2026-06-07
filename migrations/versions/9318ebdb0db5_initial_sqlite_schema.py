"""initial sqlite schema

Revision ID: 9318ebdb0db5
Revises:
Create Date: 2026-04-27 18:59:23.749668

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9318ebdb0db5'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'code_index_commit_metadata',
        sa.Column('item_id', sa.String(), nullable=False),
        sa.Column('commit_sha', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('item_id', 'commit_sha', 'key', name='pk_cicm'),
    )
    op.create_index(
        'idx_cicm_commit', 'code_index_commit_metadata', ['commit_sha', 'key'], unique=False
    )
    op.create_table(
        'code_index_file_reference',
        sa.Column('from_uuid', sa.String(), nullable=False),
        sa.Column('to_uuid', sa.String(), nullable=False),
        sa.Column('relation', sa.String(), nullable=False),
        sa.Column('meta', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('from_uuid', 'to_uuid', 'relation', name='pk_cifr'),
    )
    op.create_index('idx_cifr_to', 'code_index_file_reference', ['to_uuid'], unique=False)
    op.create_index(
        'idx_cifr_relation', 'code_index_file_reference', ['relation'], unique=False
    )
    op.create_table(
        'scheduler_tasks',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('scheduled_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(), server_default='pending', nullable=False),
        sa.Column('result', sa.Text(), server_default='', nullable=False),
        sa.Column('error', sa.Text(), server_default='', nullable=False),
        sa.Column('recurrence', sa.Text(), server_default='', nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('scheduler_tasks')
    op.drop_index('idx_cifr_relation', table_name='code_index_file_reference')
    op.drop_index('idx_cifr_to', table_name='code_index_file_reference')
    op.drop_table('code_index_file_reference')
    op.drop_index('idx_cicm_commit', table_name='code_index_commit_metadata')
    op.drop_table('code_index_commit_metadata')
