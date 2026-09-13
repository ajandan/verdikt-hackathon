"""programs.requires_review + inbox_events.callback_* — async webhook lane

Adds the pieces the hybrid REST/MCP intake contract needs:

- `programs.requires_review` (bool): when true, M2M submissions to this
  program bypass the sync verdict and are queued for reviewer approval.
- `inbox_events.callback_url` + `callback_status` + `callback_attempts` +
  `callback_delivered_at` + `callback_last_error`: per-case webhook state
  so the approve/reject endpoints know where to POST and can record
  delivery outcomes.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "programs",
        sa.Column(
            "requires_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "inbox_events",
        sa.Column("callback_url", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "inbox_events",
        sa.Column("callback_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "inbox_events",
        sa.Column(
            "callback_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "inbox_events",
        sa.Column("callback_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inbox_events",
        sa.Column("callback_last_error", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_inbox_events_callback_status",
        "inbox_events",
        "callback_status IS NULL OR callback_status IN "
        "('pending', 'delivered', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_inbox_events_callback_status", "inbox_events", type_="check")
    op.drop_column("inbox_events", "callback_last_error")
    op.drop_column("inbox_events", "callback_delivered_at")
    op.drop_column("inbox_events", "callback_attempts")
    op.drop_column("inbox_events", "callback_status")
    op.drop_column("inbox_events", "callback_url")
    op.drop_column("programs", "requires_review")
