"""inbox_events.case_number — short human-readable case identifier

Reviewers need a short handle to reference a case in chat/email — the UUID is
too long to speak. Adds a monotonic sequence-backed integer column so the UI
can render `QC-0042` style tags. NOT NULL with DEFAULT nextval so existing
rows are backfilled during the ALTER TABLE.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-11
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS inbox_case_seq START 1")
    op.add_column(
        "inbox_events",
        sa.Column(
            "case_number",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('inbox_case_seq')"),
        ),
    )
    op.create_index(
        "ix_inbox_events_case_number", "inbox_events", ["case_number"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_events_case_number", table_name="inbox_events")
    op.drop_column("inbox_events", "case_number")
    op.execute("DROP SEQUENCE IF EXISTS inbox_case_seq")
