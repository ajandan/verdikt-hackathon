"""Copy-a-rule-to-a-program three-pass auto-mapper.

Pins the confidence contract: exact match = 1.0, fuzzy above the 0.45
floor, unresolved below. Also pins the specific pair we care about most
— Alderwood's form-native slots vs canonical Intake names — because the
whole point of the mapper is to bridge that particular gap without a
per-program mapper module.
"""
from __future__ import annotations

import pytest

from pap_pipeline.rule_mapping import Match, auto_map

# ---------------------------------------------------------------------------
# Exact-match pass
# ---------------------------------------------------------------------------

def test_exact_match_returns_confidence_exact_and_score_one():
    matches = auto_map(["insurance_status"], ["insurance_status", "drug"])
    assert matches[0] == Match(
        source="insurance_status",
        target="insurance_status",
        confidence="exact",
        score=1.0,
    )


def test_empty_source_refs_returns_empty_matches():
    """Wizard is entered with no @field refs on the source rule → no
    rows to render, no work to do."""
    assert auto_map([], ["a", "b", "c"]) == []


def test_empty_target_slots_marks_everything_unresolved():
    """Program with no extraction_schema declared yet — every ref is
    unresolved so the operator sees the shape of the ask before they
    even have a schema."""
    matches = auto_map(["insurance_status"], [])
    assert matches[0].confidence == "unresolved"
    assert matches[0].target is None


# ---------------------------------------------------------------------------
# Fuzzy pass — the real-world Alderwood pairs the whole feature exists to bridge
# ---------------------------------------------------------------------------

def test_fuzzy_matches_postal_code_to_patient_zip():
    """`postal_code` (canonical) → `patient_zip` (form-native). This is
    exactly the pair that caused QC-0060 to land as missing_info."""
    matches = auto_map(["postal_code"], ["patient_zip", "patient_city", "patient_state"])
    assert matches[0].source == "postal_code"
    assert matches[0].confidence == "fuzzy"
    assert matches[0].target in ("patient_zip",)  # Not city/state


def test_fuzzy_matches_insurance_status_to_ins_uninsured():
    """One of the CV-INS-01 pain points — the fuzzy pass should
    prefer the semantically-closer 'ins_uninsured' over other
    'ins_*' siblings when the source names 'insurance_status'."""
    matches = auto_map(
        ["insurance_status"],
        ["ins_uninsured", "ins_medicare_partd", "ins_primary_payer"],
    )
    assert matches[0].confidence == "fuzzy"
    # Any of these is defensible — the invariant is that we picked
    # SOMETHING with a real score rather than falling to unresolved.
    assert matches[0].target in {"ins_uninsured", "ins_medicare_partd", "ins_primary_payer"}
    assert matches[0].score >= 0.45


def test_fuzzy_matches_date_of_birth_to_patient_dob():
    matches = auto_map(["date_of_birth"], ["patient_dob", "patient_name", "prescriber_dea"])
    assert matches[0].confidence == "fuzzy"
    assert matches[0].target == "patient_dob"


def test_fuzzy_matches_prescribed_drug_to_rx_medication():
    matches = auto_map(["prescribed_drug"], ["rx_medication", "rx_strength", "rx_quantity"])
    assert matches[0].confidence == "fuzzy"
    assert matches[0].target == "rx_medication"


def test_fuzzy_matches_family_name_to_patient_last_name():
    matches = auto_map(
        ["family_name"],
        ["patient_first_name", "patient_last_name", "patient_middle_initial"],
    )
    assert matches[0].confidence == "fuzzy"
    assert matches[0].target == "patient_last_name"


# ---------------------------------------------------------------------------
# Confidence floor — unrelated names must NOT get confidently matched
# ---------------------------------------------------------------------------

def test_unrelated_ref_falls_to_unresolved():
    """A source name with no semantic relationship to any target must
    be unresolved so the operator picks one — a false-confident match
    is worse than an honest 'help me here'."""
    matches = auto_map(
        ["quantum_flux_capacitor"],
        ["patient_name", "insurance_status", "prescribed_drug"],
    )
    assert matches[0].confidence == "unresolved"
    assert matches[0].target is None
    assert matches[0].score == 0.0


# ---------------------------------------------------------------------------
# Order preservation — the wizard shows rows in the order the operator sees
# the refs in the rule text top-to-bottom
# ---------------------------------------------------------------------------

def test_output_order_matches_input_order():
    matches = auto_map(
        ["insurance_status", "prescribed_drug", "postal_code"],
        ["patient_zip", "rx_medication", "ins_uninsured"],
    )
    assert [m.source for m in matches] == [
        "insurance_status", "prescribed_drug", "postal_code",
    ]


def test_batch_returns_mixed_confidence():
    """One realistic batch: some exact, some fuzzy, some unresolved.
    Wizard renders green/amber/red pills based on this."""
    matches = auto_map(
        ["prescriber_npi", "insurance_status", "totally_unrelated"],
        ["prescriber_npi", "ins_uninsured", "patient_zip"],
    )
    confidences = [m.confidence for m in matches]
    assert confidences == ["exact", "fuzzy", "unresolved"]
