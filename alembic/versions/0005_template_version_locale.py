"""template_versions: add locale column, replace unique constraint

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "template_versions",
        sa.Column(
            "locale",
            sa.String(length=16),
            nullable=False,
            server_default="default",
        ),
    )
    # Existing rows get "default" via server_default; drop it so app-layer default owns future inserts.
    op.alter_column("template_versions", "locale", server_default=None)

    op.drop_constraint("uq_template_version", "template_versions", type_="unique")
    op.create_unique_constraint(
        "uq_template_version_locale",
        "template_versions",
        ["template_id", "version", "locale"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_template_version_locale", "template_versions", type_="unique")
    op.create_unique_constraint(
        "uq_template_version",
        "template_versions",
        ["template_id", "version"],
    )
    op.drop_column("template_versions", "locale")
