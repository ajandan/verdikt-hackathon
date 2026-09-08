"""inbox_events: persist body_text for admin review

Adds a nullable TEXT column so operators can see the actual message content
on the review page. The poller truncates to ~8KB before insert to keep the
row small; oversized bodies show a "[truncated]" suffix.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inbox_events",
        sa.Column("body_text", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inbox_events", "body_text")
