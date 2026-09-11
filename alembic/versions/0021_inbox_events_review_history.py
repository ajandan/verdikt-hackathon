"""inbox_events.review_history — audit trail of human decisions on an event

Every approve/reject a reviewer takes appends an entry here so the reviewer
view can render "who did what, when." JSONB list of
{at, action, actor, note} objects. Nullable — legacy events pre-migration
simply have no recorded history.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-11
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inbox_events",
        sa.Column("review_history", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inbox_events", "review_history")
