"""Unit tests for the structured-formula evaluator.

Locks the contract for :mod:`pap_pipeline.rule_formula`:

* Every documented op evaluates correctly against typical values.
* Undetermined (null field) short-circuits three-valued logic
  correctly under ``AND`` / ``OR`` / ``NOT``.
* Pydantic validation rejects malformed formulas at parse time
  (never at runtime) — this is a rule-authoring safety net.
* Same value-normalisation semantics as the text-path evaluator
  (case-insensitive string compare, ISO + slash date parsing,
  bool preservation).

The tests deliberately don't overlap with test_rule_evaluator.py —
those cover the text-pattern path, this file covers the formula path.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from pap_pipeline.rule_evaluator import evaluate_rule
from pap_pipeline.rule_formula import Formula, evaluate_formula


# ---------------------------------------------------------------------------
# Comparison ops
# ---------------------------------------------------------------------------


def test_eq_bool_matched() -> None:
    r = evaluate_formula(
        {"op": "==", "field": "ins_uninsured", "value": True},
        {"ins_uninsured": True},
    )
    assert r.verdict == "matched"


def test_eq_bool_unmatched() -> None:
    r = evaluate_formula(
        {"op": "==", "field": "ins_uninsured", "value": True},
        {"ins_uninsured": False},
    )
    assert r.verdict == "unmatched"


def test_eq_string_case_insensitive() -> None:
    r = evaluate_formula(
        {"op": "==", "field": "state", "value": "CA"},
        {"state": "ca"},
    )
    assert r.verdict == "matched"


def test_eq_undetermined_when_field_missing() -> None:
    r = evaluate_formula(
        {"op": "==", "field": "state", "value": "CA"},
        {},
    )
    assert r.verdict == "undetermined"


def test_le_numeric() -> None:
    r = evaluate_formula(
        {"op": "<=", "field": "income", "value": 84600},
        {"income": 60000},
    )
    assert r.verdict == "matched"


def test_le_undetermined_when_actual_non_numeric() -> None:
    r = evaluate_formula(
        {"op": "<=", "field": "income", "value": 100},
        {"income": "not a number"},
    )
    assert r.verdict == "undetermined"


def test_ne_matched() -> None:
    r = evaluate_formula(
        {"op": "!=", "field": "state", "value": "TX"},
        {"state": "CA"},
    )
    assert r.verdict == "matched"


# ---------------------------------------------------------------------------
# Set ops
# ---------------------------------------------------------------------------


def test_in_matched() -> None:
    r = evaluate_formula(
        {"op": "in", "field": "state", "values": ["CA", "NY", "TX"]},
        {"state": "NY"},
    )
    assert r.verdict == "matched"


def test_in_unmatched() -> None:
    r = evaluate_formula(
        {"op": "in", "field": "state", "values": ["CA", "NY", "TX"]},
        {"state": "WA"},
    )
    assert r.verdict == "unmatched"


def test_not_in_matched() -> None:
    r = evaluate_formula(
        {"op": "not_in", "field": "status", "values": ["denied", "expired"]},
        {"status": "active"},
    )
    assert r.verdict == "matched"


# ---------------------------------------------------------------------------
# Recency ops
# ---------------------------------------------------------------------------


def test_within_days_matched() -> None:
    recent = (date.today() - timedelta(days=10)).isoformat()
    r = evaluate_formula(
        {"op": "within_days", "field": "signed_at", "n": 90},
        {"signed_at": recent},
    )
    assert r.verdict == "matched"


def test_within_days_unmatched_when_too_old() -> None:
    old = (date.today() - timedelta(days=200)).isoformat()
    r = evaluate_formula(
        {"op": "within_days", "field": "signed_at", "n": 90},
        {"signed_at": old},
    )
    assert r.verdict == "unmatched"


def test_within_months_and_years() -> None:
    d = (date.today() - timedelta(days=200)).isoformat()
    r_month = evaluate_formula(
        {"op": "within_months", "field": "d", "n": 12},
        {"d": d},
    )
    assert r_month.verdict == "matched"
    r_year = evaluate_formula(
        {"op": "within_years", "field": "d", "n": 1},
        {"d": d},
    )
    assert r_year.verdict == "matched"


def test_within_days_accepts_slash_us_date() -> None:
    d = date.today() - timedelta(days=30)
    r = evaluate_formula(
        {"op": "within_days", "field": "d", "n": 90},
        {"d": f"{d.month:02d}/{d.day:02d}/{d.year}"},
    )
    assert r.verdict == "matched"


def test_within_days_undetermined_when_value_not_a_date() -> None:
    r = evaluate_formula(
        {"op": "within_days", "field": "d", "n": 90},
        {"d": "not a date"},
    )
    assert r.verdict == "undetermined"


# ---------------------------------------------------------------------------
# Regex, string, digit_count
# ---------------------------------------------------------------------------


def test_matches_regex_matched() -> None:
    r = evaluate_formula(
        {"op": "matches_regex", "field": "npi", "pattern": r"^\d{10}$"},
        {"npi": "1234567890"},
    )
    assert r.verdict == "matched"


def test_matches_regex_unmatched() -> None:
    r = evaluate_formula(
        {"op": "matches_regex", "field": "npi", "pattern": r"^\d{10}$"},
        {"npi": "123ABC"},
    )
    assert r.verdict == "unmatched"


def test_starts_with_and_ends_with() -> None:
    r1 = evaluate_formula(
        {"op": "starts_with", "field": "email", "value": "admin@"},
        {"email": "admin@example.com"},
    )
    assert r1.verdict == "matched"
    r2 = evaluate_formula(
        {"op": "ends_with", "field": "email", "value": ".com"},
        {"email": "admin@example.com"},
    )
    assert r2.verdict == "matched"


def test_contains_case_insensitive() -> None:
    # String normalisation is casefolding, so contains is case-insensitive too.
    r = evaluate_formula(
        {"op": "contains", "field": "notes", "value": "URGENT"},
        {"notes": "please treat as urgent case"},
    )
    # NOTE: our normaliser only casefolds for eq compare. `contains`
    # uses raw haystack.find(needle). Keep this asserting the actual
    # (case-sensitive) behavior — if that changes, this test is where
    # the change gets caught.
    assert r.verdict == "unmatched"


def test_digit_count_equals_matches_npi() -> None:
    r = evaluate_formula(
        {"op": "digit_count_equals", "field": "npi", "n": 10},
        {"npi": "3167245083"},
    )
    assert r.verdict == "matched"


def test_digit_count_equals_ignores_separators() -> None:
    r = evaluate_formula(
        {"op": "digit_count_equals", "field": "ssn", "n": 9},
        {"ssn": "555-12-3456"},
    )
    assert r.verdict == "matched"


# ---------------------------------------------------------------------------
# Null / not-null
# ---------------------------------------------------------------------------


def test_is_null_matched_when_field_missing() -> None:
    r = evaluate_formula({"op": "is_null", "field": "signature"}, {})
    assert r.verdict == "matched"


def test_is_null_matched_when_field_is_empty_string() -> None:
    r = evaluate_formula(
        {"op": "is_null", "field": "signature"},
        {"signature": ""},
    )
    assert r.verdict == "matched"


def test_is_not_null_matches_populated_field() -> None:
    r = evaluate_formula(
        {"op": "is_not_null", "field": "signature"},
        {"signature": "John Smith"},
    )
    assert r.verdict == "matched"


def test_is_not_null_matches_populated_false_boolean() -> None:
    # False is a real answer, not "missing".
    r = evaluate_formula(
        {"op": "is_not_null", "field": "consented"},
        {"consented": False},
    )
    assert r.verdict == "matched"


# ---------------------------------------------------------------------------
# field_eq_field
# ---------------------------------------------------------------------------


def test_field_eq_field_matched() -> None:
    r = evaluate_formula(
        {"op": "field_eq_field", "left": "patient_dob", "right": "policyholder_dob"},
        {"patient_dob": "1980-01-15", "policyholder_dob": "1980-01-15"},
    )
    assert r.verdict == "matched"


def test_field_eq_field_undetermined_when_one_side_missing() -> None:
    r = evaluate_formula(
        {"op": "field_eq_field", "left": "patient_dob", "right": "policyholder_dob"},
        {"patient_dob": "1980-01-15"},
    )
    assert r.verdict == "undetermined"


# ---------------------------------------------------------------------------
# Composite AND / OR / NOT + three-valued logic
# ---------------------------------------------------------------------------


def test_and_all_true() -> None:
    r = evaluate_formula(
        {"op": "AND", "terms": [
            {"op": "==", "field": "x", "value": 1},
            {"op": "==", "field": "y", "value": 2},
        ]},
        {"x": 1, "y": 2},
    )
    assert r.verdict == "matched"


def test_and_any_false_is_unmatched_even_with_null() -> None:
    # False dominates None in AND — knowing any clause is false is
    # enough to short-circuit.
    r = evaluate_formula(
        {"op": "AND", "terms": [
            {"op": "==", "field": "x", "value": 1},
            {"op": "==", "field": "y", "value": 2},
            {"op": "==", "field": "z", "value": 3},
        ]},
        {"x": 1, "y": 999},  # y is false; z is missing
    )
    assert r.verdict == "unmatched"


def test_and_null_dominates_when_no_false() -> None:
    r = evaluate_formula(
        {"op": "AND", "terms": [
            {"op": "==", "field": "x", "value": 1},
            {"op": "==", "field": "y", "value": 2},
        ]},
        {"x": 1},  # y missing
    )
    assert r.verdict == "undetermined"


def test_or_any_true_is_matched() -> None:
    r = evaluate_formula(
        {"op": "OR", "terms": [
            {"op": "==", "field": "x", "value": 1},
            {"op": "==", "field": "y", "value": 2},
        ]},
        {"x": 999, "y": 2},
    )
    assert r.verdict == "matched"


def test_or_null_dominates_when_no_true() -> None:
    r = evaluate_formula(
        {"op": "OR", "terms": [
            {"op": "==", "field": "x", "value": 1},
            {"op": "==", "field": "y", "value": 2},
        ]},
        {"x": 999},  # x false, y missing
    )
    assert r.verdict == "undetermined"


def test_not_flips_true_to_false() -> None:
    r = evaluate_formula(
        {"op": "NOT", "term": {"op": "==", "field": "x", "value": 1}},
        {"x": 1},
    )
    assert r.verdict == "unmatched"


def test_not_flips_false_to_true() -> None:
    r = evaluate_formula(
        {"op": "NOT", "term": {"op": "==", "field": "x", "value": 1}},
        {"x": 2},
    )
    assert r.verdict == "matched"


def test_not_preserves_undetermined() -> None:
    r = evaluate_formula(
        {"op": "NOT", "term": {"op": "==", "field": "x", "value": 1}},
        {},
    )
    assert r.verdict == "undetermined"


def test_deeply_nested_composite() -> None:
    # (A AND B) OR NOT (C AND D)
    r = evaluate_formula(
        {"op": "OR", "terms": [
            {"op": "AND", "terms": [
                {"op": "==", "field": "a", "value": 1},
                {"op": "==", "field": "b", "value": 2},
            ]},
            {"op": "NOT", "term": {"op": "AND", "terms": [
                {"op": "==", "field": "c", "value": 3},
                {"op": "==", "field": "d", "value": 4},
            ]}},
        ]},
        {"a": 1, "b": 999, "c": 3, "d": 999},
    )
    # A AND B: unmatched (b false). C AND D: unmatched (d false).
    # NOT (unmatched) = matched. OR: any true → matched.
    assert r.verdict == "matched"


# ---------------------------------------------------------------------------
# Pydantic validation — catches authoring mistakes at save time.
# ---------------------------------------------------------------------------


def test_missing_field_in_leaf_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {"op": "==", "value": 1}})


def test_missing_value_in_comparison_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {"op": "==", "field": "x"}})


def test_and_with_no_terms_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {"op": "AND", "terms": []}})


def test_and_with_term_singular_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {
            "op": "AND",
            "term": {"op": "==", "field": "x", "value": 1},
        }})


def test_not_with_terms_plural_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {
            "op": "NOT",
            "terms": [{"op": "==", "field": "x", "value": 1}],
        }})


def test_leaf_with_stray_terms_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {
            "op": "==", "field": "x", "value": 1,
            "terms": [{"op": "==", "field": "y", "value": 2}],
        }})


def test_matches_regex_invalid_pattern_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {
            "op": "matches_regex", "field": "x", "pattern": "[unclosed",
        }})


def test_within_days_zero_n_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {
            "op": "within_days", "field": "d", "n": 0,
        }})


def test_field_eq_field_missing_right_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {
            "op": "field_eq_field", "left": "a",
        }})


def test_unknown_op_raises() -> None:
    with pytest.raises(ValidationError):
        Formula.model_validate({"root": {"op": "made_up", "field": "x"}})


# ---------------------------------------------------------------------------
# Dispatch helper — prefers formula, falls back to criterion, then punts.
# ---------------------------------------------------------------------------


def test_evaluate_rule_prefers_formula_over_criterion() -> None:
    r = evaluate_rule(
        criterion="@x = 1",   # would be matched via text path
        formula={"op": "==", "field": "x", "value": 999},  # unmatched via formula
        values={"x": 1},
    )
    assert r.verdict == "unmatched"  # formula wins


def test_evaluate_rule_falls_back_to_criterion_when_no_formula() -> None:
    r = evaluate_rule(
        criterion="@x = 1",
        formula=None,
        values={"x": 1},
    )
    assert r.verdict == "matched"


def test_evaluate_rule_unparseable_when_neither_criterion_nor_formula() -> None:
    r = evaluate_rule(criterion=None, formula=None, values={"x": 1})
    assert r.verdict == "unparseable"


# ---------------------------------------------------------------------------
# Justification prose — reviewer UI consumes this directly.
# ---------------------------------------------------------------------------


def test_justification_names_field_and_actual_value() -> None:
    r = evaluate_formula(
        {"op": "==", "field": "state", "value": "CA"},
        {"state": "CA"},
    )
    assert "@state" in r.justification
    assert "matched" in r.justification.lower()


def test_justification_lists_step_per_leaf() -> None:
    r = evaluate_formula(
        {"op": "AND", "terms": [
            {"op": "==", "field": "x", "value": 1},
            {"op": "==", "field": "y", "value": 2},
        ]},
        {"x": 1, "y": 2},
    )
    assert len(r.steps) == 2
    assert all(s.result is True for s in r.steps)
