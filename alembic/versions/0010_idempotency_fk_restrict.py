"""idempotency_keys.job_id: CASCADE → RESTRICT

Deleting a RenderJob previously cascaded to its IdempotencyKey mapping, which
silently broke the guarantee: a retry with the same key would allow a *new*
render (different job_id) instead of returning the original one. With RESTRICT
the operator must prune the mapping first (or wait for TTL cleanup), so a job
can't be deleted while a client might still retry against its key.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


_FK_NAME = "idempotency_keys_job_id_fkey"


def upgrade() -> None:
    op.drop_constraint(_FK_NAME, "idempotency_keys", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        source_table="idempotency_keys",
        referent_table="render_jobs",
        local_cols=["job_id"],
        remote_cols=["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "idempotency_keys", type_="foreignkey")
    op.create_foreign_key(
        _FK_NAME,
        source_table="idempotency_keys",
        referent_table="render_jobs",
        local_cols=["job_id"],
        remote_cols=["id"],
        ondelete="CASCADE",
    )
