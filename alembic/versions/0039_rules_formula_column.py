"""Structured-formula column on rules + rule_versions

Adds ``rules.formula`` (JSONB, nullable) — the canonical structured
representation of a rule's criterion, authored at design time via
LLM-assisted translation from the free-form ``criterion`` string.

When populated, the pipeline's deterministic evaluator walks the
formula tree directly (no text pattern-matching, no LLM at runtime
for this rule). When NULL, the pipeline falls back to the existing
text-criterion path — so this migration is safe on a live DB: no
existing case flow changes until formulas are populated per-rule.

Mirrors on ``rule_versions.formula`` so the compliance-grade snapshot
captures which formula was in effect at decision time; a formula
edit + a case processed under the older formula must both be
reconstructable from the version row.

Both columns are nullable + default NULL. Backfill (Chunk 7) runs a
one-time translator over existing rules; any row whose translator
returns None stays NULL and continues on the text path until
re-authored.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rules",
        sa.Column("formula", JSONB, nullable=True),
    )
    op.add_column(
        "rule_versions",
        sa.Column("formula", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rule_versions", "formula")
    op.drop_column("rules", "formula")
