"""template_versions.subversion + templates.current_subversion + template_audit_events

Editing placements on a frozen version no longer mutates the row — it clones
into a new (version, subversion+1) so v1.0 remains immutable history. Only
the highest (version, subversion) tuple for a template is editable; publish
targets a specific (version, subversion) pair.

`template_audit_events` records who did what and when. Publish events also
gain a subversion column so the existing publish history keeps rendering
correctly with the new precision.

Backfill: every existing row gets subversion=0 (server_default). Existing
templates with a current_version get current_subversion=0 (max of the frozen
subs for that version, which is 0 pre-migration).

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-02
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add subversion to template_versions. Server-default 0 backfills all
    #    existing rows, then dropped so app-layer default owns future inserts
    #    (same pattern as 0011/0012).
    op.add_column(
        "template_versions",
        sa.Column(
            "subversion",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("template_versions", "subversion", server_default=None)

    # Swap unique constraint to include subversion. Two frozen sub-versions
    # for the same (version, locale) must have distinct subversion ints.
    op.drop_constraint(
        "uq_template_version_locale",
        "template_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_template_version_locale",
        "template_versions",
        ["template_id", "version", "subversion", "locale"],
    )

    # 2. current_subversion on templates — which sub of current_version is
    #    live. Nullable: null means "no sub-version pinned" (legacy rows or
    #    unpublished templates).
    op.add_column(
        "templates",
        sa.Column("current_subversion", sa.Integer(), nullable=True),
    )
    # Backfill: any template with a published current_version gets subversion 0
    # (pre-migration there was only one sub per version).
    op.execute(
        "UPDATE templates SET current_subversion = 0 WHERE current_version IS NOT NULL"
    )

    # 3. subversion columns on publish_events so publish history stays precise.
    op.add_column(
        "publish_events",
        sa.Column("subversion", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("publish_events", "subversion", server_default=None)
    op.add_column(
        "publish_events",
        sa.Column("previous_subversion", sa.Integer(), nullable=True),
    )

    # 4. Audit trail table.
    op.create_table(
        "template_audit_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "template_id",
            UUID(as_uuid=True),
            sa.ForeignKey("templates.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Nullable because some events (e.g. "template created") apply to the
        # template as a whole, not any single version row.
        sa.Column(
            "template_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("template_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Denormalized for display when the FK is null or the version row
        # is later deleted — the audit trail must survive schema churn.
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("subversion", sa.Integer(), nullable=True),
        sa.Column("locale", sa.String(length=16), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column("event_kind", sa.String(length=32), nullable=False),
        sa.Column("summary", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_events_template_created",
        "template_audit_events",
        ["template_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_template_created", table_name="template_audit_events")
    op.drop_table("template_audit_events")
    op.drop_column("publish_events", "previous_subversion")
    op.drop_column("publish_events", "subversion")
    op.drop_column("templates", "current_subversion")
    op.drop_constraint("uq_template_version_locale", "template_versions", type_="unique")
    op.create_unique_constraint(
        "uq_template_version_locale",
        "template_versions",
        ["template_id", "version", "locale"],
    )
    op.drop_column("template_versions", "subversion")
