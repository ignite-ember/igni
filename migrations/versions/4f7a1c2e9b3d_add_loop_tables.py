"""add loop state + progress tables

Revision ID: 4f7a1c2e9b3d
Revises: 9318ebdb0db5
Create Date: 2026-06-04 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4f7a1c2e9b3d'
down_revision: Union[str, Sequence[str], None] = '9318ebdb0db5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'loop_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('iteration_index', sa.Integer(), nullable=False),
        sa.Column('iterations_remaining', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_loop_state_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'loop_progress',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', sa.Text(), server_default='', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'key', name='uq_loop_progress_run_key'),
    )
    op.create_index(
        op.f('ix_loop_progress_run_id'),
        'loop_progress',
        ['run_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_loop_progress_run_id'), table_name='loop_progress')
    op.drop_table('loop_progress')
    op.drop_table('loop_state')
