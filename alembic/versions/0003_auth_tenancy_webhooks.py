"""auth, tenancy scoping, webhook fields

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-06
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    op.execute(sa.text(
        "INSERT INTO tenants (id, name, ai_provider_config) "
        "VALUES (:id, 'default', '{}'::jsonb) ON CONFLICT (name) DO NOTHING"
    ).bindparams(id=DEFAULT_TENANT_ID))

    op.add_column("templates", sa.Column(
        "tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(sa.text(
        "UPDATE templates SET tenant_id = :tid WHERE tenant_id IS NULL"
    ).bindparams(tid=DEFAULT_TENANT_ID))
    op.alter_column("templates", "tenant_id", nullable=False)
    op.create_foreign_key(
        "fk_templates_tenant", "templates", "tenants",
        ["tenant_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_templates_tenant", "templates", ["tenant_id"])

    op.add_column("render_jobs", sa.Column(
        "tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(sa.text(
        "UPDATE render_jobs rj SET tenant_id = t.tenant_id "
        "FROM template_versions tv JOIN templates t ON t.id = tv.template_id "
        "WHERE rj.template_version_id = tv.id AND rj.tenant_id IS NULL"
    ))
    op.alter_column("render_jobs", "tenant_id", nullable=False)
    op.create_foreign_key(
        "fk_render_jobs_tenant", "render_jobs", "tenants",
        ["tenant_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_render_jobs_tenant", "render_jobs", ["tenant_id"])

    op.add_column("render_jobs", sa.Column("callback_url", sa.String(1024)))
    op.add_column("render_jobs", sa.Column(
        "webhook_state", sa.String(32), nullable=False, server_default="not_configured"))
    op.add_column("render_jobs", sa.Column("webhook_last_error", sa.Text))
    op.add_column("render_jobs", sa.Column("webhook_attempts", sa.Integer, nullable=False, server_default="0"))

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(12), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_api_keys_tenant", "api_keys", ["tenant_id"])
    op.create_index("ix_api_keys_hash", "api_keys", ["key_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_api_keys_hash", table_name="api_keys")
    op.drop_index("ix_api_keys_tenant", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_column("render_jobs", "webhook_attempts")
    op.drop_column("render_jobs", "webhook_last_error")
    op.drop_column("render_jobs", "webhook_state")
    op.drop_column("render_jobs", "callback_url")
    op.drop_index("ix_render_jobs_tenant", table_name="render_jobs")
    op.drop_constraint("fk_render_jobs_tenant", "render_jobs", type_="foreignkey")
    op.drop_column("render_jobs", "tenant_id")
    op.drop_index("ix_templates_tenant", table_name="templates")
    op.drop_constraint("fk_templates_tenant", "templates", type_="foreignkey")
    op.drop_column("templates", "tenant_id")
