"""rule_versions — immutable audit history per rule

Every rule save (create + update) writes one row into this table. Backfill
seeds a version 1 for every existing rule so history isn't blank on day one.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("public_rule_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("criterion", sa.String(length=400), nullable=False),
        sa.Column(
            "exception", sa.String(length=400), nullable=False, server_default=""
        ),
        sa.Column(
            "fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_username", sa.String(length=255), nullable=False, server_default="system"
        ),
        sa.Column(
            "change_note", sa.String(length=200), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "rule_id", "version", name="uq_rule_versions_rule_version"
        ),
    )
    op.create_index(
        "ix_rule_versions_rule_id", "rule_versions", ["rule_id"]
    )

    # Backfill: seed v1 for every existing rule so the history card isn't
    # empty. We stamp the actor as "system" and a change note that records
    # the origin so it's obvious in the UI these weren't authored via a save.
    op.execute(
        sa.text(
            """
            INSERT INTO rule_versions (
                id, rule_id, version, public_rule_id, kind, title, criterion,
                exception, fields, is_active, actor_user_id, actor_username,
                change_note, created_at
            )
            SELECT
                gen_random_uuid(), r.id, 1, r.rule_id, r.kind, r.title,
                r.criterion, r.exception, r.fields, r.is_active,
                NULL, 'system', 'Backfilled from migration 0027',
                COALESCE(r.updated_at, r.created_at, now())
            FROM rules r
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_rule_versions_rule_id", table_name="rule_versions")
    op.drop_table("rule_versions")
