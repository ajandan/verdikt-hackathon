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

Renumbered from a second "0039": two migrations shared that id, so a clean
database failed with "Multiple head revisions". Databases already stamped
0039 may have these columns from before the rename, so the upgrade adds
them only when missing.

Both columns are nullable + default NULL. Backfill (Chunk 7) runs a
one-time translator over existing rules; any row whose translator
returns None stays NULL and continues on the text path until
re-authored.

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-20
"""
from __future__ import annotations

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE rules ADD COLUMN IF NOT EXISTS formula JSONB")
    op.execute("ALTER TABLE rule_versions ADD COLUMN IF NOT EXISTS formula JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE rule_versions DROP COLUMN IF EXISTS formula")
    op.execute("ALTER TABLE rules DROP COLUMN IF EXISTS formula")
