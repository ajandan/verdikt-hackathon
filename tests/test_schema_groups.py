"""Slot-name → reviewer-group bucketing tests.

Covers the deterministic prefix + exact-name mapping used to render the
extract card as grouped sections on the pipeline detail page. No IO, no
program row required — pure function against slot-name strings.
"""
from __future__ import annotations

from pap_pipeline.schema_groups import GROUP_ORDER, bucket_slots, group_for


def test_exact_name_overrides_beat_generic_other() -> None:
    # No prefix, but we know the semantic — must not fall through to Other.
    assert group_for("patient_name") == "Patient"
    assert group_for("date_of_birth") == "Patient"
    assert group_for("prescribed_drug") == "Prescription"
    assert group_for("has_denial_letter") == "Insurance"


def test_prefix_match_covers_common_pap_conventions() -> None:
    assert group_for("ins_uninsured") == "Insurance"
    assert group_for("ins_primary_payer") == "Insurance"
    assert group_for("rx_medication") == "Prescription"
    assert group_for("rx_days_supply") == "Prescription"
    assert group_for("dx_primary_icd10") == "Diagnosis"
    assert group_for("prescriber_npi") == "Prescriber"
    assert group_for("patient_address_line1") == "Patient"
    assert group_for("household_size") == "Household & finance"
    assert group_for("sig_patient_date") == "Signatures"
    assert group_for("req_pap") == "Requests"


def test_longer_prefix_wins_over_shorter() -> None:
    # `annual_household_income` must land under Household & finance, not fall
    # through to Other by matching neither `household_` nor an exact override.
    assert group_for("annual_household_income") == "Household & finance"


def test_unknown_slot_falls_through_to_other() -> None:
    assert group_for("weird_custom_field") == "Other"
    assert group_for("foo") == "Other"


def test_bucket_slots_preserves_input_order_within_group() -> None:
    # Insertion order is meaningful — the pipeline populates the extraction
    # dict in schema-declared order and we want the group to reflect that.
    slots = ["rx_medication", "rx_strength", "rx_dosage_form", "rx_sig"]
    result = bucket_slots(slots)
    assert result == [("Prescription", slots)]


def test_bucket_slots_returns_groups_in_canonical_order() -> None:
    # Input order shuffled across groups; output order must follow GROUP_ORDER,
    # not the input arrival order.
    slots = [
        "prescriber_npi",           # Prescriber
        "patient_name",             # Patient
        "household_size",           # Household & finance
        "ins_uninsured",            # Insurance
        "rx_medication",            # Prescription
    ]
    result = bucket_slots(slots)
    ordered_group_names = [g for g, _ in result]
    # Patient comes before Household & finance which comes before Insurance
    # which comes before Prescription which comes before Prescriber.
    assert ordered_group_names == [
        "Patient",
        "Household & finance",
        "Insurance",
        "Prescription",
        "Prescriber",
    ]


def test_bucket_slots_drops_empty_groups() -> None:
    # Nothing in Diagnosis / Signatures / Requests / Other — those must not
    # show up as empty sections on the reviewer page.
    result = bucket_slots(["patient_name", "prescribed_drug"])
    names = [g for g, _ in result]
    assert "Patient" in names
    assert "Prescription" in names
    assert "Other" not in names
    assert "Signatures" not in names


def test_bucket_slots_handles_empty_input() -> None:
    assert bucket_slots([]) == []


def test_bucket_slots_puts_unknowns_in_other_bucket() -> None:
    slots = ["patient_name", "some_custom_thing", "rx_medication"]
    result = bucket_slots(slots)
    other_group = dict(result).get("Other")
    assert other_group == ["some_custom_thing"]


def test_group_order_contains_other_last() -> None:
    # Structural guard — Other must be the last group so unknowns don't
    # visually dominate the well-known clinical/financial sections.
    assert GROUP_ORDER[-1] == "Other"
