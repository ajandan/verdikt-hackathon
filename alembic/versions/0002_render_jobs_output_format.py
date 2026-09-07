"""render_jobs: add output_format column

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-06
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "render_jobs",
        sa.Column("output_format", sa.String(length=16), nullable=False, server_default="docx"),
    )


def downgrade() -> None:
    op.drop_column("render_jobs", "output_format")
