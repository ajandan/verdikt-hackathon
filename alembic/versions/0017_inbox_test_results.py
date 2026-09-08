"""inbox_test_results: per-ingest AI test-render trials

Each accepted ingest now runs N (default 5) synthesized-payload render trials
so the reviewer can see how the frozen template behaves across varied inputs
rather than trusting a single sample. Every trial persists its input payload,
the rendered artifact blob key, and an AI-authored pass/fail verdict so the
same data can drive:

  - the email reply's summary table (with per-row download links), and
  - the admin portal's inbox detail page (same table, same links).

`inbox_event_id` cascades on delete because the trial rows are worthless
without the parent event; there's no cross-event reuse.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_test_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "inbox_event_id",
            UUID(as_uuid=True),
            sa.ForeignKey("inbox_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 1-indexed slot number within the parent event's trial set. Lets the
        # UI show a stable "Test 1 of 5" label independent of insert order.
        sa.Column("ordinal", sa.Integer(), nullable=False),
        # Short human-friendly name the planner chose ("happy path",
        # "long strings", "unicode"). Renders in the summary table header.
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("data_json", JSONB(), nullable=False),
        # Blob key of the rendered output (docx or pdf). Null when the render
        # itself failed — we still keep the row so the verdict is auditable.
        sa.Column("blob_key", sa.String(length=512), nullable=True),
        sa.Column("ext", sa.String(length=8), nullable=True),
        # Verdict is the AI's one-sentence explanation of *why* it judged the
        # render output good or bad. Kept as free text so reviewers can read
        # it inline; `passed` is the machine-parseable summary.
        sa.Column("verdict", sa.Text(), nullable=False, server_default=""),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "inbox_event_id", "ordinal", name="uq_inbox_test_results_event_ordinal"
        ),
    )
    op.create_index(
        "ix_inbox_test_results_event_id",
        "inbox_test_results",
        ["inbox_event_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_test_results_event_id", table_name="inbox_test_results"
    )
    op.drop_table("inbox_test_results")
