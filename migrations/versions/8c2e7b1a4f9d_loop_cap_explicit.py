"""loop_state.cap_explicit column

Revision ID: 8c2e7b1a4f9d
Revises: 4f7a1c2e9b3d
Create Date: 2026-06-04 00:00:00.000000

Distinguishes a *safety net* cap (``/loop <prompt>`` — the user
expressed no expectation about how many iterations) from an
*explicit* cap (``/loop N <prompt>`` — the user said exactly N).
Auto-extend at cap-hit only fires for implicit caps; explicit
caps still terminate at the user's N.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8c2e7b1a4f9d'
down_revision: Union[str, Sequence[str], None] = '4f7a1c2e9b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows (if any) get ``False`` — treating them as
    # implicit is the safer default since auto-extend keeps the
    # loop alive instead of cutting it short.
    op.add_column(
        'loop_state',
        sa.Column('cap_explicit', sa.Boolean(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('loop_state', 'cap_explicit')
