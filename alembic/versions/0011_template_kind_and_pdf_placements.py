"""templates.kind + template_versions.pdf_flavor / pdf_field_placements

PDF templates are a first-class Template.kind alongside 'docx'. Two PDF flavors:
  - 'acroform': fill widgets by name (no coordinates needed)
  - 'flat':     draw overlays at stored coordinates (pdf_field_placements)

Both PDF columns are nullable — populated only when kind='pdf'. Existing rows
are backfilled to kind='docx' via server_default, which is then dropped so the
app-layer default owns future inserts (matches the 0005 locale pattern).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-01
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "templates",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="docx",
        ),
    )
    op.alter_column("templates", "kind", server_default=None)

    op.add_column(
        "template_versions",
        sa.Column("pdf_flavor", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "template_versions",
        sa.Column("pdf_field_placements", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("template_versions", "pdf_field_placements")
    op.drop_column("template_versions", "pdf_flavor")
    op.drop_column("templates", "kind")
