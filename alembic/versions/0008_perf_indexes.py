"""performance indexes for hot dashboard + provenance queries

Adds btree indexes on the columns dashboards and provenance list queries
already filter or order by. All additive; no data migration.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-23
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dashboard stats/spark/recents/top-templates/failing-jobs all filter
    # RenderJob by (tenant_id) and range/order on submitted_at.
    op.create_index(
        "ix_render_jobs_tenant_submitted",
        "render_jobs",
        ["tenant_id", "submitted_at"],
    )
    # Foreign key lookup; also drives the /provenance join and "top templates"
    # aggregation. FK columns aren't auto-indexed in Postgres.
    op.create_index(
        "ix_render_jobs_template_version",
        "render_jobs",
        ["template_version_id"],
    )
    # Provenance list is filtered by template_version_id and ordered by
    # rendered_at desc. Composite covers both in one lookup.
    op.create_index(
        "ix_provenance_template_version_rendered",
        "provenance",
        ["template_version_id", "rendered_at"],
    )
    # FK lookup by render_job_id — used when correlating a completed render to
    # its provenance row. Also needed once we add H5's idempotency guard.
    op.create_index(
        "ix_provenance_render_job",
        "provenance",
        ["render_job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_provenance_render_job", table_name="provenance")
    op.drop_index("ix_provenance_template_version_rendered", table_name="provenance")
    op.drop_index("ix_render_jobs_template_version", table_name="render_jobs")
    op.drop_index("ix_render_jobs_tenant_submitted", table_name="render_jobs")
