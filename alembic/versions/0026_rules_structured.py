"""rules structured — split text into title + criterion + exception

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-12
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


# For each seeded rule_id, the structured replacement values. Any rule row
# whose rule_id isn't in this map (e.g. custom rules added via the UI) gets a
# best-effort fallback: title = first sentence of text (truncated), criterion
# = the rest (truncated), exception = ''.
_STRUCTURED: dict[str, dict[str, str]] = {
    "CV-INC-01": {
        "title": "Household income at or below 400% FPL for household size",
        "criterion": "`annual_household_income` ≤ 400% × 2026 FPL(`household_size`)",
        "exception": "If `req_pap` is checked but ASP-110 worksheet is missing or has blank fields, return missing_info instead of deny.",
    },
    "CV-INS-01": {
        "title": "Applicant lacks adequate prescription drug coverage",
        "criterion": "`insurance_status` ∈ {uninsured, Medicare without Part D, commercial with `has_denial_letter`=true}",
        "exception": "Medicaid enrollees are ineligible. Commercial insurance requires an attached denial letter.",
    },
    "CV-CLIN-01": {
        "title": "Current Cardiovita Rx from active U.S. prescriber with valid NPI",
        "criterion": "`prescribed_drug` = 'Cardiovita' AND `prescriber_npi` active in NPPES AND `prescription_date` within last 12 months",
        "exception": "Off-label indications allowed if documented in `dx_primary_description` / `dx_primary_icd10`. Signature stamps not accepted.",
    },
    "CV-RES-01": {
        "title": "U.S. resident with valid physical mailing address",
        "criterion": "`country_of_residence` = 'US' AND `mailing_state` ∈ U.S. state or territory (PR, GU, VI, AS, MP)",
        "exception": "P.O. Box alone is not sufficient — physical street address required for cold-chain signature-on-delivery.",
    },
    "OB-INC-01": {
        "title": "Household income at or below 250% FPL for household size",
        "criterion": "`annual_household_income` ≤ 250% × 2026 FPL(`household_size`)",
        "exception": "If either `household_size` or `annual_household_income` is blank, return missing_info instead of deny.",
    },
    "OB-INS-01": {
        "title": "Active prescription-drug coverage gap during bridge window",
        "criterion": "`insurance_status` ∈ {uninsured, between plans (open enrollment / COBRA), commercial with `has_denial_letter`=true}",
        "exception": "Medicaid enrollees ineligible. Medicare Part D eligible only with a written denial letter for this therapy.",
    },
    "OB-CLIN-01": {
        "title": "Current Rx (any FDA-approved outpatient drug) from active U.S. prescriber",
        "criterion": "`prescribed_drug` FDA-approved for outpatient dispensing AND `prescriber_npi` active in NPPES AND `prescription_date` within last 6 months",
        "exception": "Signature stamps not accepted — prescriber countersignature on OB-200 Section 6 is required.",
    },
    "OB-RES-01": {
        "title": "U.S. resident with valid mailing address",
        "criterion": "`country_of_residence` = 'US' AND `mailing_state` ∈ U.S. state or territory (PR, GU, VI, AS, MP)",
        "exception": "P.O. Box accepted — OncoBridge ships ambient-temperature fulfillments.",
    },
}


def _fallback(text: str) -> dict[str, str]:
    """Best-effort split for user-created rules that aren't in the seed map."""
    stripped = " ".join((text or "").split())
    # Take first sentence as title (≤200), rest as criterion (≤400).
    first_period = stripped.find(". ")
    if 0 < first_period < 200:
        title = stripped[:first_period].strip()
        rest = stripped[first_period + 2 :].strip()
    else:
        title = stripped[:200].rstrip()
        rest = stripped[200:].lstrip()
    return {
        "title": title or "Untitled rule",
        "criterion": (rest[:400].rstrip() or title),
        "exception": "",
    }


def upgrade() -> None:
    # Phase 1: add columns nullable so we can backfill.
    op.add_column("rules", sa.Column("title", sa.String(length=200), nullable=True))
    op.add_column("rules", sa.Column("criterion", sa.String(length=400), nullable=True))
    op.add_column(
        "rules",
        sa.Column("exception", sa.String(length=400), nullable=False, server_default=""),
    )

    # Phase 2: backfill from the seed map (for known rule_ids) or from text.
    bind = op.get_bind()
    rows = list(bind.execute(sa.text("SELECT id, rule_id, text FROM rules")).mappings())
    for r in rows:
        packed: dict[str, Any] = dict(_STRUCTURED.get(r["rule_id"]) or _fallback(r["text"] or ""))
        bind.execute(
            sa.text(
                "UPDATE rules SET title = :title, criterion = :criterion, exception = :exception "
                "WHERE id = :id"
            ),
            {"id": r["id"], **packed},
        )

    # Phase 3: enforce NOT NULL on title + criterion; drop the old text column.
    op.alter_column("rules", "title", nullable=False)
    op.alter_column("rules", "criterion", nullable=False)
    op.drop_column("rules", "text")


def downgrade() -> None:
    op.add_column("rules", sa.Column("text", sa.Text(), nullable=True))
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE rules SET text = title || E'\n\n' || criterion || "
            "CASE WHEN exception <> '' THEN E'\n\nException: ' || exception ELSE '' END"
        )
    )
    op.alter_column("rules", "text", nullable=False)
    op.drop_column("rules", "exception")
    op.drop_column("rules", "criterion")
    op.drop_column("rules", "title")
