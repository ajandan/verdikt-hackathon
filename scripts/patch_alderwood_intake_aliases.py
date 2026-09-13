"""Patch the Alderwood extraction_schema with `intake_field` slot aliases.

The Alderwood program's extraction_schema uses form-oriented slot names
(``rx_medication``, ``ins_uninsured``, ``patient_dob``). The canonical
:class:`pap_pipeline.schemas.intake.Intake` model — the shape rules
evaluate against — uses different names (``prescribed_drug``,
``insurance_status``, ``date_of_birth``). Without a mapping, every
Alderwood case lands in ``missing_info`` because the rule-required
Intake fields are always null.

This script sets ``intake_field`` (and, where the source is a boolean
that maps to an enum, ``intake_value_map``) on the relevant slots so
:func:`pap_pipeline.json_intake.validate_json_intake` mirrors the coerced
value onto the canonical Intake field name.

Idempotent — re-running only overwrites the two alias keys, never other
slot metadata.

Run:
    python scripts/patch_alderwood_intake_aliases.py
Optional env:
    ALDERWOOD_TENANT_NAME  override the tenant name
    ALDERWOOD_PROGRAM_SLUG override the program slug (default: cardiovita-pap-2026)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy.orm.attributes import flag_modified

from docgen.store.db import SyncSessionLocal
from docgen.store.models import Program, Tenant

# slot_name -> {intake_field, [intake_value_map]}
_ALIASES: dict[str, dict[str, object]] = {
    "rx_medication": {"intake_field": "prescribed_drug"},
    "rx_date_written": {"intake_field": "prescription_date"},
    "patient_dob": {"intake_field": "date_of_birth"},
    "patient_last_name": {"intake_field": "family_name"},
    "patient_state": {"intake_field": "mailing_state"},
    "patient_address_line1": {"intake_field": "address_line1"},
    "patient_address_line2": {"intake_field": "address_line2"},
    "patient_city": {"intake_field": "city"},
    "patient_zip": {"intake_field": "postal_code"},
    "internal_referral_id": {"intake_field": "referral_id"},
    # ins_uninsured is a boolean checkbox on the form; the canonical
    # `insurance_status` is an enum. Only the True side maps — leave
    # False alone so a caller who unchecks the box doesn't stamp
    # "commercial" onto insurance_status.
    "ins_uninsured": {
        "intake_field": "insurance_status",
        "intake_value_map": {"true": "uninsured"},
    },
}

_TENANT_NAME = os.environ.get(
    "ALDERWOOD_TENANT_NAME", "Alderwood Specialty Pharmacy"
)
_PROGRAM_SLUG = os.environ.get(
    "ALDERWOOD_PROGRAM_SLUG", "cardiovita-pap-2026"
)


def main() -> int:
    with SyncSessionLocal() as session:
        tenant = (
            session.query(Tenant)
            .filter(Tenant.name == _TENANT_NAME)
            .one_or_none()
        )
        if tenant is None:
            print(f"tenant not found: {_TENANT_NAME!r}", file=sys.stderr)
            return 1

        program = (
            session.query(Program)
            .filter(
                Program.tenant_id == tenant.id,
                Program.slug == _PROGRAM_SLUG,
            )
            .one_or_none()
        )
        if program is None:
            print(
                f"program {_PROGRAM_SLUG!r} not found for tenant {tenant.name!r}",
                file=sys.stderr,
            )
            return 1

        schema = list(program.extraction_schema or [])
        if not schema:
            print("program has no extraction_schema — nothing to patch")
            return 0

        patched = 0
        for slot in schema:
            name = (slot.get("name") or "").strip()
            spec = _ALIASES.get(name)
            if not spec:
                continue
            for k, v in spec.items():
                slot[k] = v
            patched += 1

        program.extraction_schema = schema
        flag_modified(program, "extraction_schema")
        session.commit()

    print(
        f"patched {patched} slot(s) on {_TENANT_NAME!r} / {_PROGRAM_SLUG!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
