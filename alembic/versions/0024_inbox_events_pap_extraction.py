"""inbox_events.pap_extraction — raw per-program extraction dict

The orchestrator coerces a Program's dynamic extraction dict onto :class:`Intake`
so the reason stage keeps a fixed field signature, but that boundary silently
drops slots that don't overlap the canonical Intake schema. This column stores
the *raw* extraction dict as returned by the extract stage so:

  - the pipeline detail page can render every slot the operator defined
    (not just the ones Intake happens to know about);
  - the reason stage can be handed non-Intake fields as "extras" to reason over;
  - the reply mapper can map program-specific slots onto template fields whose
    names match, without those slots having to be added to Intake.

Nullable — pre-migration events keep their existing pap_intake payload and
render fine on the detail page (the template falls back to pap_intake when
pap_extraction is null).

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-11
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inbox_events",
        sa.Column("pap_extraction", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inbox_events", "pap_extraction")
