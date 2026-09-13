"""history rules — allow kind='history' + seed CV-HIS-01/02 into every tenant

Two changes in one migration:

1. Extend :data:`ck_rules_kind` to allow ``'history'``. The existing four
   kinds map cleanly to intake-derived fields (financial, insurance,
   residency, clinical); ``history`` is the new bucket for rules that
   evaluate against MDM-derived history slots (ADR-0004).

2. Seed two history rules into every existing tenant's rules library and
   link them to that tenant's ``cardiovita-pap-2026`` program row (if any):

   * **CV-HIS-01** — deny duplicate enrollment while a prior approval is
     still active.
   * **CV-HIS-02** — fast-path renewal when a prior approval is
     within 90 days of renewal.

   New tenants pick these up via the shipped YAML packs
   (``src/pap_pipeline/rules/CV-HIS-*.yaml``) at the time their programs
   are seeded; this migration is the backfill for tenants that already
   exist.

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-13
"""
from __future__ import annotations

import json
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


# Hard-coded in the migration (not read from YAML) so the seed is stable
# across future edits to the shipped YAML packs. If ops want to edit these
# for a specific tenant, that's what /ui/rules is for.
_HISTORY_RULES = [
    {
        "rule_id": "CV-HIS-01",
        "kind": "history",
        "title": "No duplicate enrollment while a prior approval is active",
        "criterion": (
            "If `prior_enrollment_active` = true, deny — the active "
            "enrollment remains in force through its renewal date."
        ),
        "exception": (
            "If the clinical indication has materially changed vs. the "
            "prior enrollment, route to missing_info for a clinical-change "
            "attestation instead of denying."
        ),
        "fields": ["prior_enrollment_active"],
    },
    {
        "rule_id": "CV-HIS-02",
        "kind": "history",
        "title": "Fast-path renewal for members with expiring approvals",
        "criterion": (
            "If `prior_enrollment_active` = true AND "
            "`renewal_due_within_90d` = true, approve on the renewal track."
        ),
        "exception": (
            "If any intake field differs from the prior enrollment record, "
            "fall through to CV-INC-01 / CV-RES-01 evaluation."
        ),
        "fields": ["prior_enrollment_active", "renewal_due_within_90d"],
    },
]

_PROGRAM_SLUG = "cardiovita-pap-2026"


def upgrade() -> None:
    op.drop_constraint("ck_rules_kind", "rules", type_="check")
    op.create_check_constraint(
        "ck_rules_kind",
        "rules",
        "kind IN ('financial', 'insurance', 'residency', 'clinical', 'history')",
    )

    conn = op.get_bind()
    tenants = conn.execute(sa.text("SELECT id FROM tenants")).fetchall()

    # Seed each history rule into every tenant's library. Existing rows
    # (from a re-run or a partial upgrade) are left alone via ON CONFLICT.
    seeded_by_tenant: dict[str, dict[str, str]] = {}
    for (tenant_id,) in tenants:
        for rule in _HISTORY_RULES:
            new_id = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    """
                    INSERT INTO rules
                        (id, tenant_id, rule_id, kind, title, criterion,
                         exception, fields, is_active)
                    VALUES
                        (:id, :tid, :rid, :kind, :title, :criterion,
                         :exception, CAST(:fields AS jsonb), true)
                    ON CONFLICT (tenant_id, rule_id) DO NOTHING
                    """
                ),
                {
                    "id": new_id,
                    "tid": str(tenant_id),
                    "rid": rule["rule_id"],
                    "kind": rule["kind"],
                    "title": rule["title"],
                    "criterion": rule["criterion"],
                    "exception": rule["exception"],
                    "fields": json.dumps(rule["fields"]),
                },
            )
            row = conn.execute(
                sa.text(
                    "SELECT id FROM rules WHERE tenant_id = :tid AND rule_id = :rid"
                ),
                {"tid": str(tenant_id), "rid": rule["rule_id"]},
            ).first()
            if row is not None:
                seeded_by_tenant.setdefault(str(tenant_id), {})[rule["rule_id"]] = str(row[0])

    # Link each seeded history rule to the tenant's cardiovita-pap-2026
    # program row, if the program exists. Position is appended after the
    # existing rule links so history rules show up last in the reviewer view.
    programs = conn.execute(
        sa.text(
            "SELECT id, tenant_id FROM programs WHERE slug = :slug AND is_active = true"
        ),
        {"slug": _PROGRAM_SLUG},
    ).fetchall()

    for program_id, tenant_id in programs:
        rule_map = seeded_by_tenant.get(str(tenant_id)) or {}
        if not rule_map:
            continue
        # Compute the base position from what's already linked so we append.
        max_pos = conn.execute(
            sa.text(
                "SELECT COALESCE(MAX(position), -1) FROM program_rule_links "
                "WHERE program_id = :pid"
            ),
            {"pid": str(program_id)},
        ).scalar() or -1
        for offset, rule in enumerate(_HISTORY_RULES):
            rules_row_id = rule_map.get(rule["rule_id"])
            if not rules_row_id:
                continue
            conn.execute(
                sa.text(
                    """
                    INSERT INTO program_rule_links
                        (id, program_id, rule_id, position, is_active)
                    VALUES
                        (:id, :pid, :rid, :pos, true)
                    ON CONFLICT (program_id, rule_id) DO NOTHING
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "pid": str(program_id),
                    "rid": rules_row_id,
                    "pos": max_pos + 1 + offset,
                },
            )


def downgrade() -> None:
    conn = op.get_bind()
    # Detach and delete only the rows this migration seeded — leave any
    # ops-authored rules with the same public rule_id alone (unlikely, but
    # a downgrade shouldn't guess).
    conn.execute(
        sa.text(
            "DELETE FROM program_rule_links WHERE rule_id IN ("
            "  SELECT id FROM rules WHERE rule_id IN ('CV-HIS-01', 'CV-HIS-02')"
            ")"
        )
    )
    conn.execute(
        sa.text(
            "DELETE FROM rules WHERE rule_id IN ('CV-HIS-01', 'CV-HIS-02')"
        )
    )
    op.drop_constraint("ck_rules_kind", "rules", type_="check")
    op.create_check_constraint(
        "ck_rules_kind",
        "rules",
        "kind IN ('financial', 'insurance', 'residency', 'clinical')",
    )
