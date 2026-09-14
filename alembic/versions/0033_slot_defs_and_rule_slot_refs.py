"""programs.history_slots + rules.references_slots — ADR-0004 PR2

Two additive columns land the slot-compiler seam:

- ``programs.history_slots`` (JSONB, default ``[]``) — declared slot
  definitions for the program pack. Each entry is a dict shaped like
  ``{"name": "...", "kind": "bool" | "latest_outcome" | ..., "where": {...}}``
  consumed by :mod:`pap_pipeline.stages.slot_compile`.
- ``rules.references_slots`` / ``rule_versions.references_slots``
  (JSONB, nullable) — list of slot names this rule reads. Nullable
  because history-agnostic rules don't touch slots and shouldn't be
  forced to carry an empty list.

The publish gate on the program admin route reads both to reject
activation when a rule references a slot the program hasn't declared.
Nothing writes to these columns yet from the admin UI — a future PR
adds the editor surfaces. Backfill for existing history rules
(CV-HIS-01/02 seeded in 0031) reuses their ``fields`` list to
populate ``references_slots`` so the gate has data to validate the
day this ships.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-14
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "programs",
        sa.Column(
            "history_slots",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "rules",
        sa.Column("references_slots", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "rule_versions",
        sa.Column("references_slots", sa.dialects.postgresql.JSONB(), nullable=True),
    )

    # Backfill: history-kind rules that already carry slot names in `fields`
    # (the seeded CV-HIS-01 / CV-HIS-02) get their references_slots populated
    # from that same list. Non-history rules stay NULL so a subsequent gate
    # check doesn't hallucinate slot dependencies where none exist.
    op.execute(
        """
        UPDATE rules
           SET references_slots = fields
         WHERE kind = 'history'
           AND fields IS NOT NULL
           AND jsonb_typeof(fields) = 'array'
           AND jsonb_array_length(fields) > 0
        """
    )
    op.execute(
        """
        UPDATE rule_versions
           SET references_slots = fields
         WHERE kind = 'history'
           AND fields IS NOT NULL
           AND jsonb_typeof(fields) = 'array'
           AND jsonb_array_length(fields) > 0
        """
    )

    # Seed program.history_slots for the cardiovita-pap-2026 program so the
    # already-linked history rules have declared slots to validate against.
    # Kept minimal: two slots the CV-HIS-* rules reference. Ops can extend
    # via the admin UI once the editor surface lands.
    _seed_defs = (
        '[{"name":"prior_enrollment_active","kind":"bool",'
        '"where":{"event_type":["determination","enrollment"],'
        '"outcome":["approve"]}},'
        '{"name":"renewal_due_within_90d","kind":"bool",'
        '"where":{"event_type":["enrollment","determination"],'
        '"outcome":["approve"],"within_days":365}}]'
    )
    op.execute(
        sa.text(
            "UPDATE programs SET history_slots = CAST(:defs AS jsonb) "
            "WHERE slug = 'cardiovita-pap-2026'"
        ).bindparams(defs=_seed_defs)
    )


def downgrade() -> None:
    op.drop_column("rule_versions", "references_slots")
    op.drop_column("rules", "references_slots")
    op.drop_column("programs", "history_slots")
