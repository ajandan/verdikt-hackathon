"""templates.is_active soft-delete flag

Inactive templates disappear from listings (UI + MCP) and refuse to render.
Versions, blobs, and provenance stay intact so an operator can reactivate
without losing history — this is archive, not hard delete.

Existing rows are backfilled to is_active=true via server_default, which is
then dropped so the app-layer default owns future inserts (matches the 0011
pattern).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-02
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "templates",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.alter_column("templates", "is_active", server_default=None)


def downgrade() -> None:
    op.drop_column("templates", "is_active")
