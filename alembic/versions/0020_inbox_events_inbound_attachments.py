"""inbox_events.inbound_attachments — persist raw inbound files for reviewer preview

The PAP pipeline processed inbound attachments in-memory only, so the reviewer
had no way to see what the applicant actually sent. This adds a JSONB manifest
column of {filename, content_type, blob_key, size} entries, populated by the
PAP handler when it uploads each attachment to the documents bucket before
running the pipeline. Column is nullable so template-flow events and
pre-migration pipeline events coexist without backfill.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-11
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inbox_events",
        sa.Column("inbound_attachments", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inbox_events", "inbound_attachments")
