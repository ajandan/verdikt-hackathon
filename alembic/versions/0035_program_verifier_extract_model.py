"""programs.verifier_extract_model — optional dual-VLM cross-check

Third nullable string column on ``programs`` for the Phase C dual-VLM
extraction verifier. When set, the pipeline runs a second extract stage
in parallel with the primary using this model and diffs the outputs; any
disagreement on a critical slot escalates the case to HumanLoop.

Kept separate from ``extract_model`` / ``reason_model`` because "verifier"
is optional insurance for high-stakes tenants — leaving it NULL keeps
the pipeline single-VLM (the previous behavior).

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-16
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "programs",
        sa.Column("verifier_extract_model", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("programs", "verifier_extract_model")
