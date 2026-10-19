"""The demo program's income rule, exactly as the seed ships it.

The email test guide promises three answers for the Maria Chen packet:
approve with both PDFs, missing_info without the ASP-110 worksheet, and a
denial when income is over the cap. These tests pin the shipped formula to
those answers, so a seed edit cannot quietly break the judge-facing demo.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pap_pipeline.rule_formula import Formula, evaluate_formula
from pap_pipeline.verify import drop_out_of_range

SEED = Path(__file__).resolve().parents[1] / "deploy" / "demo-seed" / "seed.json"


def _seed() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


def _income_rule() -> dict:
    return next(r for r in _seed()["rules"] if r["rule_id"] == "CV-INC-01-cardio")


def _verdict(**values):
    return evaluate_formula(_income_rule()["formula"], values).verdict


def test_formula_validates() -> None:
    Formula.model_validate({"root": _income_rule()["formula"]})


def test_sample_packet_passes() -> None:
    # Maria Chen: household of 2, $60,000. 2026 cap for 2 is 4 x $21,640.
    assert _verdict(household_size=2, annual_household_income=60000) == "matched"


def test_amount_printed_as_text_still_reads() -> None:
    assert _verdict(household_size="2", annual_household_income="$60,000") == "matched"


def test_over_the_cap_fails() -> None:
    assert _verdict(household_size=2, annual_household_income=86561) == "unmatched"
    assert _verdict(household_size=2, annual_household_income=86560) == "matched"


@pytest.mark.parametrize("size,cap", [(1, 63840), (4, 132000), (8, 222880), (10, 268320)])
def test_cap_scales_with_household_size(size: int, cap: int) -> None:
    assert _verdict(household_size=size, annual_household_income=cap) == "matched"
    assert _verdict(household_size=size, annual_household_income=cap + 1) == "unmatched"


def test_missing_worksheet_is_undetermined_not_a_denial() -> None:
    # Test 2 in the guide: ASP-100 only, so neither worksheet field is read.
    assert _verdict(household_size=None, annual_household_income=None) == "undetermined"
    assert _verdict(household_size=2, annual_household_income=None) == "undetermined"


def test_rule_is_an_approval_gate_for_the_demo_program() -> None:
    rule = _income_rule()
    prog = _seed()["programs"][0]
    assert rule["on_match"] == "approve" and rule["is_active"]
    assert rule["program_id"] == prog["id"]
    slots = {s["name"]: s for s in prog["extraction_schema"]}
    assert slots["household_size"]["type"] == "integer"
    assert slots["annual_household_income"]["type"] == "number"


def test_household_size_outside_the_table_goes_to_review_not_denial() -> None:
    # The income table covers 1 to 10. A size outside it (the scanned packet
    # had the Rx quantity "30" read as household size) must not deny.
    slots = _seed()["programs"][0]["extraction_schema"]
    size = next(s for s in slots if s["name"] == "household_size")
    assert (size["min"], size["max"]) == (1, 10)
    raw, findings = drop_out_of_range(
        {"household_size": 30, "annual_household_income": 33800}, slots
    )
    assert [f.type for f in findings] == ["extract_out_of_range"]
    assert _verdict(**raw) == "undetermined"

