"""tenants.quota_* — per-tenant usage caps

Adds three nullable columns on ``tenants``. ``NULL`` means "no cap" so
existing tenants stay unlimited unless an operator opts them in.

- ``quota_renders_per_day``    — rolling 24h count of render jobs
- ``quota_active_templates``   — count of ``templates`` with ``is_active=true``
- ``quota_storage_bytes``      — reserved column; enforcement TODO (sums
  template-version bytes + render-result bytes; ADR needed on how to
  attribute doc-package storage across the two)

Renders / active-templates are enforced by ``docgen.quotas``; storage
bytes has no enforcement path in this migration.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-14
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("quota_renders_per_day", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("quota_active_templates", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("quota_storage_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "quota_storage_bytes")
    op.drop_column("tenants", "quota_active_templates")
    op.drop_column("tenants", "quota_renders_per_day")
