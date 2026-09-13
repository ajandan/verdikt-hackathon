"""Deterministic bucketing of extraction slot names into reviewer-facing groups.

The reviewer's pipeline detail page renders one card per group instead of a
40-item flat list. Grouping is done at render time from slot names alone —
no schema change, no operator setup, no AI call. Slice 2 will add persisted
groups on the Program row so an operator can override the defaults; slice 3
will add an AI-suggested regroup for schemas whose slot names don't follow
any convention. This module is the shared shape all three slices agree on:
`list[tuple[str, list[str]]]` — ordered groups, insertion-order slots
within each group.
"""
from __future__ import annotations

from collections.abc import Iterable

GROUP_ORDER: list[str] = [
    "Patient",
    "Caregiver",
    "Household & finance",
    "Insurance",
    "Prescription",
    "Diagnosis",
    "Clinical",
    "Prior therapy",
    "Prescriber",
    "Practice",
    "Residency",
    "Shipping",
    "Services requested",
    "Requests",
    "Contact preferences",
    "Consents",
    "Signatures",
    "Internal",
    "Other",
]

# Bare slot names (no prefix) whose semantics we know from the demo schemas.
# Anything not listed here or matched by a prefix below lands in "Other".
_EXACT_NAME_GROUP: dict[str, str] = {
    "patient_name": "Patient",
    "date_of_birth": "Patient",
    "annual_household_income": "Household & finance",
    "household_size": "Household & finance",
    "insurance_status": "Insurance",
    "has_denial_letter": "Insurance",
    "prescribed_drug": "Prescription",
    "prescription_date": "Prescription",
    "country_of_residence": "Residency",
    "mailing_state": "Residency",
}

# Longest prefix first so `annual_household_` beats `household_`,
# `contact_pref_` beats a hypothetical `contact_`, etc.
_PREFIX_GROUP: list[tuple[str, str]] = [
    ("annual_household_", "Household & finance"),
    ("household_", "Household & finance"),
    ("prior_therapy_", "Prior therapy"),
    ("contact_pref_", "Contact preferences"),
    ("prescriber_", "Prescriber"),
    ("caregiver_", "Caregiver"),
    ("practice_", "Practice"),
    ("clinical_", "Clinical"),
    ("internal_", "Internal"),
    ("consent_", "Consents"),
    ("ship_to_", "Shipping"),
    ("mailing_", "Residency"),
    ("patient_", "Patient"),
    ("ins_", "Insurance"),
    ("svc_", "Services requested"),
    ("rx_", "Prescription"),
    ("dx_", "Diagnosis"),
    ("sig_", "Signatures"),
    ("req_", "Requests"),
]


def group_for(slot_name: str) -> str:
    """Return the group label for one slot name."""
    exact = _EXACT_NAME_GROUP.get(slot_name)
    if exact is not None:
        return exact
    for prefix, group in _PREFIX_GROUP:
        if slot_name.startswith(prefix):
            return group
    return "Other"


def bucket_slots(slot_names: Iterable[str]) -> list[tuple[str, list[str]]]:
    """Bucket slot names into ordered groups.

    Returns groups in GROUP_ORDER; empty groups are dropped. Within a group,
    slot order preserves the input iteration order — so if the caller passes
    keys from a dict that was populated in schema-declared order, the group's
    render order also reflects that schema order.
    """
    buckets: dict[str, list[str]] = {name: [] for name in GROUP_ORDER}
    for slot in slot_names:
        buckets[group_for(slot)].append(slot)
    return [(g, buckets[g]) for g in GROUP_ORDER if buckets[g]]


__all__ = ["GROUP_ORDER", "bucket_slots", "group_for"]
