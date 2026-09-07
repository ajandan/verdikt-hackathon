"""provenance: add render_stack_hash column

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provenance",
        sa.Column("render_stack_hash", sa.String(length=16), nullable=True),
    )
    op.create_index(
        "ix_provenance_render_stack_hash",
        "provenance",
        ["render_stack_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_provenance_render_stack_hash", table_name="provenance")
    op.drop_column("provenance", "render_stack_hash")
