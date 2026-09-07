"""publish_events: audit trail for freeze→publish separation (§Phase 3)

Publishing is now distinct from freezing: freeze locks the artifact,
publish flips the version-serving pointer. Every publish/rollback lands
one row here so the timeline of what was live when is queryable.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publish_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "template_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("templates.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column(
            "previous_version",
            sa.Integer,
            nullable=True,
            comment="version this publish replaced; NULL for the first publish",
        ),
        sa.Column("event_kind", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_publish_events_template_created",
        "publish_events",
        ["template_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_publish_events_template_created", table_name="publish_events")
    op.drop_table("publish_events")
