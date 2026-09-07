"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-06
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("key_ref", sa.String(255)),
        sa.Column("ai_provider_config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("owner", sa.String(255)),
        sa.Column("current_version", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "template_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("schema", postgresql.JSONB, nullable=False),
        sa.Column("preflight", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("blob_key", sa.String(512), nullable=False),
        sa.Column("version_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("is_frozen", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("template_id", "version", name="uq_template_version"),
    )
    op.create_index("ix_template_versions_hash", "template_versions", ["version_hash"])

    op.create_table(
        "render_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("template_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("template_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("caller", sa.String(255)),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("result_blob_key", sa.String(512)),
        sa.Column("result_bytes", sa.BigInteger),
        sa.Column("error", sa.Text),
    )

    op.create_table(
        "provenance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("render_job_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("render_jobs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("template_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("template_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_hash", sa.String(64), nullable=False),
        sa.Column("caller", sa.String(255)),
        sa.Column("rendered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_provenance_hash", "provenance", ["version_hash"])


def downgrade() -> None:
    op.drop_index("ix_provenance_hash", table_name="provenance")
    op.drop_table("provenance")
    op.drop_table("render_jobs")
    op.drop_index("ix_template_versions_hash", table_name="template_versions")
    op.drop_table("template_versions")
    op.drop_table("templates")
    op.drop_table("tenants")
