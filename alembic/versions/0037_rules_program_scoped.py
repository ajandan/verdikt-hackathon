"""Rules can be copied to a program with its own vocabulary

Adds:

* ``rules.program_id`` (nullable UUID FK). NULL = library rule that lives in
  the tenant's shared master pack. Non-NULL = program-scoped copy whose
  criterion/exception text references the target program's slot names.
* ``rules.field_refs`` (JSONB) — cached list of ``@fieldname`` tokens
  parsed from the rule text. Written on save so the rules list can display
  "touches N slots" and the reasoner prompt-builder can pull the slot list
  without re-parsing.

Program-scoped rules exist because different programs' extraction schemas
speak different vocabularies (Alderwood's ASP-100 uses ``ins_uninsured``;
OncoBridge might use ``patient_ins_status``). A library rule authored in
one vocabulary can be *copied* into a program via a copy-and-remap flow
which substitutes the referenced field names.

Existing rows migrate cleanly: every current row is a library rule
(``program_id`` remains NULL) and ``field_refs`` defaults to ``[]`` on
insert — the rules service backfills it on first save after this deploy.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-17
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable FK — SET NULL on delete so removing a program leaves its
    # rules as tenant-scoped orphans the operator can archive or re-copy
    # rather than losing rule text irreversibly.
    op.add_column(
        "rules",
        sa.Column("program_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_rules_program_id",
        "rules", "programs",
        ["program_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_rules_program_id", "rules", ["program_id"])

    # Cached @field references parsed from criterion + exception on save.
    # Empty list default so the .append path in the write path is safe on
    # migrating rows that predate the field_refs concept.
    op.add_column(
        "rules",
        sa.Column(
            "field_refs",
            JSONB,
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_rules_program_id", table_name="rules")
    op.drop_constraint("fk_rules_program_id", "rules", type_="foreignkey")
    op.drop_column("rules", "program_id")
    op.drop_column("rules", "field_refs")
