"""programs.extract_model + programs.reason_model — tenant-configurable models

Two nullable string columns on the ``programs`` table. When NULL, the pipeline
falls back to the process-wide default from settings (env vars). When set, the
Extract and Reason stages read the program's chosen model and pass it to the
corresponding client config.

This is the schema side of the "per-tenant model choice" surface — the admin UI
exposes dropdowns populated from Nebius Token Factory's ``/v1/models`` catalog
so operators can swap Nemotron scale or vision model without a code deploy.

Nullable strings so:
  - existing rows work unchanged (fall back to settings)
  - the pipeline never crashes on a missing / empty value
  - a tenant can clear the override by setting to NULL again

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-16
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "programs",
        sa.Column("extract_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "programs",
        sa.Column("reason_model", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("programs", "reason_model")
    op.drop_column("programs", "extract_model")
