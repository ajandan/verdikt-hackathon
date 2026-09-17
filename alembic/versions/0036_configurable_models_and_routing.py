"""Configurable pipeline models + triage routing at tenant AND program level

Introduces a proper 3-level fallback chain for every pipeline stage's model:

    program.<stage>_model  →  tenant.<stage>_model  →  settings.<STAGE>_MODEL

Tenants gain default columns for all five stages so an operator can pin,
e.g., "this tenant uses the small extract model everywhere" without
touching each program. Programs already had ``extract_model`` (0034),
``reason_model`` (0034), and ``verifier_extract_model`` (0035); this
migration adds ``triage_model`` alongside them for symmetry.

``triage_routing`` (JSONB) is added at both levels so an operator can
rewire the case_type → downstream mapping without a code change (e.g.,
"route refills through CRM instead of Pharmacy OMS"). NULL means "inherit
from the next level up"; the resolver walks the chain.

All columns are nullable. Existing rows (which don't have these columns
today) will read as NULL and fall through to the platform defaults, so
runtime behavior is unchanged until an operator sets a value.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-16
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- tenants: default model per stage ---------------------------------
    op.add_column(
        "tenants",
        sa.Column("extract_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("triage_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("retrieve_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("reason_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("verifier_model", sa.String(length=255), nullable=True),
    )
    # Triage routing table (case_type → route). NULL = inherit platform
    # defaults defined in pap_pipeline.stages.triage._ROUTE_BY_CASE_TYPE.
    op.add_column(
        "tenants",
        sa.Column("triage_routing", JSONB, nullable=True),
    )

    # --- programs: triage model + routing override ------------------------
    # extract_model / reason_model / verifier_extract_model already exist
    # on programs (0034 + 0035). Adding triage_model here so all five
    # stages have a symmetric override slot at the program level.
    op.add_column(
        "programs",
        sa.Column("triage_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "programs",
        sa.Column("triage_routing", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("programs", "triage_routing")
    op.drop_column("programs", "triage_model")
    op.drop_column("tenants", "triage_routing")
    op.drop_column("tenants", "verifier_model")
    op.drop_column("tenants", "reason_model")
    op.drop_column("tenants", "retrieve_model")
    op.drop_column("tenants", "triage_model")
    op.drop_column("tenants", "extract_model")
