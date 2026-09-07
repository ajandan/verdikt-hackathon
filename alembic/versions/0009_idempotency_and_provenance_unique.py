"""idempotency_keys table + unique(provenance.render_job_id)

Two related hardening steps that share a migration:

1. `idempotency_keys(tenant_id, key)` stores the mapping from an
   Idempotency-Key header to the RenderJob it originally created, so a retry
   of the same POST /render returns the original job instead of spawning a
   duplicate. Payload hash lets us reject re-use with a different body.

2. Unique on `provenance.render_job_id` — the render actor gets retried on
   failure, and prior to this the second attempt could write a duplicate
   Provenance row. With the constraint, an ON CONFLICT DO NOTHING (or a
   caught IntegrityError) makes the actor idempotent.

If any duplicate Provenance rows exist in dev DBs, the constraint will fail
to create. Dedupe first:

    DELETE FROM provenance a USING provenance b
    WHERE a.render_job_id = b.render_job_id
      AND a.rendered_at > b.rendered_at;

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-23
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "job_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("render_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "key", name="pk_idempotency_keys"),
    )
    # Cleanup path: prune rows older than the configured retention.
    op.create_index(
        "ix_idempotency_keys_created_at",
        "idempotency_keys",
        ["created_at"],
    )

    op.create_unique_constraint(
        "uq_provenance_render_job",
        "provenance",
        ["render_job_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_provenance_render_job", "provenance", type_="unique"
    )
    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
