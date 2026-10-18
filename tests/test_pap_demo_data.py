"""Smoke tests for the canonical demo intake fixture.

The fixture is load-bearing — the video, the four rule tests, and the future
OncoBridge cameo all depend on the same patient having the same numbers.
These tests exist so an unintentional edit to the YAML (or a schema change
that silently drops a field) trips a red build instead of a broken demo.
"""
from __future__ import annotations

from datetime import date

from pap_pipeline.demo_data import load_canonical_intake


def test_canonical_intake_loads_and_validates() -> None:
    """The YAML fixture must round-trip through the Intake schema without loss."""
    intake = load_canonical_intake()
    assert intake.patient_name == "Maria Chen"
    assert intake.date_of_birth == date(1978, 6, 14)


def test_canonical_intake_has_every_field_the_four_rules_need() -> None:
    """Every rule's `fields` list must be fully populated by the fixture.

    If a rule declares it needs `annual_household_income` and the fixture
    leaves it None, the reason stage would treat that rule as unevaluable and
    the demo would produce `missing_info` instead of the approve the video is
    scripted around.
    """
    intake = load_canonical_intake()
    for field_name in (
        "household_size",
        "annual_household_income",
        "insurance_status",
        "has_denial_letter",
        "country_of_residence",
        "mailing_state",
        "prescribed_drug",
        "prescriber_npi",
        "prescription_date",
    ):
        assert getattr(intake, field_name) is not None, (
            f"canonical_intake.yaml leaves {field_name!r} unset; "
            "the demo scenario would not evaluate cleanly."
        )


def test_income_lands_inside_the_cameo_window() -> None:
    """The income figure must approve Cardiovita AND deny the planned OncoBridge cap.

    2026 HHS poverty guideline for a household of 2: $21,640.
    Cardiovita CV-INC-01: 400% for HH2 = $86,560.
    OncoBridge OB-INC-01: 250% for HH2 = $54,100.
    Income must satisfy 54,100 < income <= 86,560 for the cameo flip to work.
    If this test fails, the video's cameo beat is no longer accurate.
    """
    intake = load_canonical_intake()
    assert intake.household_size == 2, "cameo thresholds below assume household of 2"
    assert intake.annual_household_income is not None
    assert 54_100 < intake.annual_household_income <= 86_560, (
        f"income={intake.annual_household_income} breaks the approve/deny flip; "
        "must lie in the (250% FPL, 400% FPL] band for household of 2."
    )
