"""inbox_events.reply_attachment_blob_key — link the sent PDF to the event

The pipeline handler renders a per-outcome letter PDF and attaches it to the
outbound reply, but the bytes were discarded after send — the reviewer had no
way to see the exact document the applicant received. We now upload the
rendered PDF to the documents bucket and stash the key on the event so the
pipeline detail page can offer a download link. Column is nullable so
template-flow events and pre-migration pipeline events coexist.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-10
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inbox_events",
        sa.Column("reply_attachment_blob_key", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inbox_events", "reply_attachment_blob_key")
