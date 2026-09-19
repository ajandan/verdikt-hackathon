"""Backfill ``rule_versions.on_match`` for pre-migration snapshots.

Migration 0038 added ``on_match`` to both ``rules`` and ``rule_versions``
but the backfill it shipped only updated ``rules`` — the version
snapshot table was left at the ``server_default='approve'`` value for
every historical row. That means "what did the pipeline see when it
decided this case" reconstructions for pre-0038 decisions render every
rule as an approve-gate, including CV-HIS-01 (a deny-gate) and
CV-HIS-02 (a shortcircuit). Silent audit-trail lie.

This migration fixes the backfill by matching each existing
``rule_versions`` row against its parent rule (via the ``rule_id`` UUID
foreign key) and copying whatever ``on_match`` the parent now carries.
That's the best-available approximation — the schema didn't record
polarity at snapshot time before 0038, so "what the parent has now" is
the closest truthful signal.

Idempotent: repeatable UPDATE with the same source-of-truth join.
Running it a second time is a no-op modulo ``updated_at`` semantics
(the column isn't touched by this migration).

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Copy the parent rule's on_match onto every rule_versions row that
    # still carries the 0038 default. Restrict the WHERE to rows where
    # the parent's on_match diverges from the version's — a no-op update
    # otherwise (postgres would still touch the row via UPDATE, but the
    # visible state is unchanged).
    op.execute(
        """
        UPDATE rule_versions rv
        SET on_match = r.on_match
        FROM rules r
        WHERE rv.rule_id = r.id
          AND rv.on_match <> r.on_match;
        """
    )


def downgrade() -> None:
    # No-op: reverting would push every snapshot back to 'approve',
    # which was itself the lie this migration corrects. Downgrading past
    # 0039 leaves the audit trail in the healed state — the caller
    # continues with 0038's downgrade to drop the column entirely.
    pass
