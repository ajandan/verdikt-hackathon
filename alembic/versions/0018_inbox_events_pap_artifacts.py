"""inbox_events.pap_intake / pap_decision — persist pipeline artifacts

The template flow only needs a score + review_state to satisfy the reviewer's
question ("is this OK to publish?"). The PAP pipeline produces a richer
verdict — extracted intake fields, rule citations with match verdicts and
reranker scores, a missing-fields list, and a one-sentence rationale — and the
reviewer needs to see every layer to trust the outcome.

We persist the full Intake and Decision as JSONB alongside the existing
event-level flags so the pipeline detail page can render them without
re-running the pipeline. Both columns are nullable so template-flow events
(and any pipeline event predating this migration) coexist without backfill.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-10
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inbox_events",
        sa.Column("pap_intake", JSONB(), nullable=True),
    )
    op.add_column(
        "inbox_events",
        sa.Column("pap_decision", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inbox_events", "pap_decision")
    op.drop_column("inbox_events", "pap_intake")
