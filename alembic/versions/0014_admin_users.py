"""admin_users table: per-tenant UI login identities

Introduces database-backed admin users so tenants can be given their own
login instead of everyone sharing the single env-var admin. `tenant_id` is
nullable — a null tenant means the row is a super_admin (cross-tenant),
otherwise the user is pinned to exactly one tenant's data.

The env-var admin (`DOCGEN_ADMIN_USER` + `_PASSWORD_HASH`) stays as a
break-glass super_admin path. If this table is empty on startup and the env
vars are present, the operator can still sign in as that identity — treated
as super_admin — so a fresh deploy cannot lock itself out.

No data backfill: existing sessions minted from the env-var admin remain
valid (they carry the username, which login continues to accept via the
break-glass path). The management UI will populate this table going forward.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-05
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # Null = super_admin (no tenant scope). tenant_admin rows must have a
        # tenant; enforced by the check constraint below.
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("username", name="uq_admin_users_username"),
        sa.CheckConstraint(
            "role IN ('super_admin', 'tenant_admin')",
            name="ck_admin_users_role",
        ),
        sa.CheckConstraint(
            "(role = 'super_admin' AND tenant_id IS NULL) OR "
            "(role = 'tenant_admin' AND tenant_id IS NOT NULL)",
            name="ck_admin_users_role_tenant",
        ),
    )
    op.create_index("ix_admin_users_tenant_id", "admin_users", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_users_tenant_id", table_name="admin_users")
    op.drop_table("admin_users")
