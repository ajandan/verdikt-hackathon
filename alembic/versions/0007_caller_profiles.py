"""caller_profiles: caller-side field mapping profiles

Persists (tenant, template, version, caller, name) -> mapping dict + last sample.
The wizard authors these once per calling app; /render/mapped translates the
caller's native JSON at request time so no code changes are needed on the caller.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "caller_profiles",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "template_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("templates.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("caller", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "mapping",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "sample_payload",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.UniqueConstraint(
            "tenant_id",
            "template_id",
            "version",
            "caller",
            "name",
            name="uq_caller_profile",
        ),
    )
    op.create_index(
        "ix_caller_profiles_template_caller",
        "caller_profiles",
        ["template_id", "caller"],
    )


def downgrade() -> None:
    op.drop_index("ix_caller_profiles_template_caller", table_name="caller_profiles")
    op.drop_table("caller_profiles")
