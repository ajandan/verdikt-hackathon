"""Rule polarity: how a matched=true verdict influences overall outcome

Adds ``rules.on_match`` — the load-bearing signal that lets the pipeline
derive a decision from citations mechanically instead of trusting a
free-form LLM synthesis. Three values, each corresponding to a real
rule shape in the CardioVita library:

* ``approve`` — positive gate. ``matched=true`` means the applicant
  meets one of several requirements that all have to be true to
  approve. ``matched=false`` short-circuits to deny.
  Example: CV-INS-01 (must be uninsured / on Medicare-no-Part-D / …).

* ``deny`` — negative gate. ``matched=true`` means the applicant hit a
  blocker; verdict flips to deny regardless of other rules.
  Example: CV-HIS-01 (if prior enrollment active, deny).

* ``approve_shortcircuit`` — fast-path override. ``matched=true``
  short-circuits to approve regardless of other rules.
  Example: CV-HIS-02 (renewal fast-path when both flags true).

Without this field the pipeline can't correctly compose an outcome from
a mixed set of matched/unmatched citations (matched=true on a "deny"
rule means the opposite of matched=true on an "approve" rule). Today
that composition is delegated to the LLM, which QC-0069 proved gets it
wrong on real cases.

Existing rows default to ``approve`` (positive gate) — the most common
shape and the safest default: an incorrectly-defaulted rule contributes
to a stricter verdict, not a laxer one.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-18
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rules",
        sa.Column(
            "on_match",
            sa.String(32),
            nullable=False,
            server_default="approve",
        ),
    )
    op.create_check_constraint(
        "ck_rules_on_match",
        "rules",
        "on_match IN ('approve', 'deny', 'approve_shortcircuit')",
    )
    # Mirror on rule_versions so a polarity change is captured in the
    # compliance-grade snapshot the same way title/criterion changes are.
    # Older snapshots default to 'approve' — the safest value if we ever
    # have to reconstruct "what did the pipeline see" for a pre-migration
    # decision.
    op.add_column(
        "rule_versions",
        sa.Column(
            "on_match",
            sa.String(32),
            nullable=False,
            server_default="approve",
        ),
    )
    # Backfill the CardioVita library rules to their intended polarity.
    # Everything else stays at the ``approve`` default. If the deploy
    # runs on a fresh DB with no seed rules yet, these updates are a
    # no-op — no harm done.
    op.execute(
        """
        UPDATE rules SET on_match = 'deny'
        WHERE rule_id IN ('CV-HIS-01');
        """
    )
    op.execute(
        """
        UPDATE rules SET on_match = 'approve_shortcircuit'
        WHERE rule_id IN ('CV-HIS-02');
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_rules_on_match", "rules", type_="check")
    op.drop_column("rules", "on_match")
    op.drop_column("rule_versions", "on_match")
