"""inbox agent: per-tenant inbound config + inbox_events audit log

Adds two pieces:

1. `tenants.inbound_config` JSONB (default {}) — holds sender allow-list,
   optional subject-tag prefix, auto-publish threshold, and extra notify
   addresses. Keys the agent reads:
     - sender_domains: [str]      # allow-list (case-insensitive)
     - subject_tag: str | None    # e.g. "[tenant:acme]"; matched first
     - auto_approve_threshold: float  # 0..100; publish when score >= this
     - notify_addresses: [str]    # extra CC on every reply

2. `inbox_events` table — one row per received email the poller sees,
   whether or not it resolves to a tenant. Doubles as the source of truth
   for the review UI and for reply-to-approve matching (via
   `reply_message_id`). `tenant_id` is nullable so we can log the "unknown
   sender" case without dropping evidence.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "inbound_config",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "inbox_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("message_id", sa.String(length=512), nullable=False),
        sa.Column("from_addr", sa.String(length=320), nullable=False),
        sa.Column("to_addr", sa.String(length=320), nullable=True),
        sa.Column("cc_addrs", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("subject", sa.String(length=998), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column(
            "template_id",
            UUID(as_uuid=True),
            sa.ForeignKey("templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "template_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("template_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("review_state", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reply_message_id", sa.String(length=512), nullable=True),
        sa.Column("attachment_name", sa.String(length=255), nullable=True),
        sa.Column("attachment_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # A single Message-ID must land in the table exactly once. Poller
        # relies on this to be idempotent across restarts.
        sa.UniqueConstraint("message_id", name="uq_inbox_events_message_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'skipped')",
            name="ck_inbox_events_status",
        ),
        sa.CheckConstraint(
            "review_state IS NULL OR review_state IN "
            "('auto_approved', 'pending_review', 'approved', 'rejected')",
            name="ck_inbox_events_review_state",
        ),
    )
    op.create_index("ix_inbox_events_tenant_id", "inbox_events", ["tenant_id"])
    op.create_index("ix_inbox_events_status", "inbox_events", ["status"])
    op.create_index(
        "ix_inbox_events_reply_message_id",
        "inbox_events",
        ["reply_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_events_reply_message_id", table_name="inbox_events")
    op.drop_index("ix_inbox_events_status", table_name="inbox_events")
    op.drop_index("ix_inbox_events_tenant_id", table_name="inbox_events")
    op.drop_table("inbox_events")
    op.drop_column("tenants", "inbound_config")
