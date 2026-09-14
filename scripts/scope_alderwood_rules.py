"""Scope Alderwood's cardiovita-pap-2026 rules pack to the fields the form collects.

The default YAML pack ships four eligibility rules — CV-CLIN-01 (clinical),
CV-INS-01 (insurance), CV-INC-01 (household FPL), CV-RES-01 (US residency)
— plus history rules. Alderwood's paper enrollment form doesn't collect
household size / income or country_of_residence, so every case lands in
``missing_info`` because those two rules are always unevaluable.

This script (a) deactivates every ``program_rule_links`` row for the
Alderwood cardiovita-pap-2026 program that isn't CV-CLIN-01 or CV-INS-01
and (b) trims ``has_denial_letter`` from CV-INS-01's ``fields`` list —
the field is only meaningful for commercial applicants and Alderwood's
form doesn't collect it. The criterion text still references it, so
Nemotron will still use it if present, but won't flag its absence as
``missing_info`` for uninsured applicants (the majority PAP path).
The library rows themselves stay active — other programs may still use
them; only the Alderwood links + Alderwood's CV-INS-01 fields are edited.

Idempotent — re-running only sets ``is_active=False`` where already
false. Reactivating is a matter of flipping the flag back or running
the seed migration.

Run:
    python scripts/scope_alderwood_rules.py
Optional env:
    ALDERWOOD_TENANT_NAME
    ALDERWOOD_PROGRAM_SLUG (default: cardiovita-pap-2026)
    ALDERWOOD_KEEP_RULES   (default: CV-CLIN-01,CV-INS-01)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy.orm.attributes import flag_modified

from docgen.store.db import SyncSessionLocal
from docgen.store.models import Program, ProgramRuleLink, Rule, Tenant

# Fields to strip from specific rules on this tenant. Keyed by public
# rule_id; the criterion text stays, so Nemotron can still use these
# fields when a caller happens to supply them — we just stop declaring
# them required for missing_info bookkeeping.
_TRIM_FIELDS: dict[str, tuple[str, ...]] = {
    "CV-INS-01": ("has_denial_letter",),
}

# Criterion overrides. The default YAML pack references runtime checks
# (e.g. "prescriber_npi active in NPPES") that Nemotron interprets as
# "must be verified externally before deciding", so it flips the rule to
# matched=false even when the field is present. We can't run an NPPES
# lookup at design time, so the Alderwood copy of the rule is softened
# to key on presence/shape only — the reviewer or a downstream service
# validates the NPI before the reply goes out.
_CRITERION_OVERRIDES: dict[str, str] = {
    "CV-CLIN-01": (
        "`prescribed_drug` is 'Cardiovita' AND `prescriber_npi` is a "
        "10-digit number AND `prescription_date` is within the last 12 "
        "months of today. Treat any 10-digit NPI as valid; downstream "
        "NPPES lookup happens after reviewer approval."
    ),
}

_TENANT_NAME = os.environ.get(
    "ALDERWOOD_TENANT_NAME", "Alderwood Specialty Pharmacy"
)
_PROGRAM_SLUG = os.environ.get(
    "ALDERWOOD_PROGRAM_SLUG", "cardiovita-pap-2026"
)
_KEEP = tuple(
    r.strip()
    for r in os.environ.get("ALDERWOOD_KEEP_RULES", "CV-CLIN-01,CV-INS-01").split(",")
    if r.strip()
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
                f"program {_PROGRAM_SLUG!r} not found for {tenant.name!r}",
                file=sys.stderr,
            )
            return 1

        rows = (
            session.query(Rule, ProgramRuleLink)
            .join(ProgramRuleLink, ProgramRuleLink.rule_id == Rule.id)
            .filter(ProgramRuleLink.program_id == program.id)
            .all()
        )

        deactivated: list[str] = []
        kept: list[str] = []
        trimmed: list[str] = []
        for rule, link in rows:
            if rule.rule_id in _KEEP:
                if not link.is_active:
                    link.is_active = True
                kept.append(rule.rule_id)
                drop = _TRIM_FIELDS.get(rule.rule_id) or ()
                if drop:
                    current = list(rule.fields or [])
                    new_fields = [f for f in current if f not in drop]
                    if new_fields != current:
                        rule.fields = new_fields
                        flag_modified(rule, "fields")
                        trimmed.append(
                            f"{rule.rule_id} -{','.join(drop)}"
                        )
                override = _CRITERION_OVERRIDES.get(rule.rule_id)
                if override and rule.criterion != override:
                    rule.criterion = override
                    trimmed.append(f"{rule.rule_id} criterion")
            else:
                if link.is_active:
                    link.is_active = False
                    deactivated.append(rule.rule_id)
                else:
                    deactivated.append(f"{rule.rule_id} (already inactive)")

        session.commit()

    print(f"tenant : {_TENANT_NAME}")
    print(f"program: {_PROGRAM_SLUG}")
    print(f"kept   : {', '.join(sorted(kept)) or '(none)'}")
    print(f"scoped out: {', '.join(sorted(deactivated)) or '(none)'}")
    print(f"trimmed: {', '.join(sorted(trimmed)) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
