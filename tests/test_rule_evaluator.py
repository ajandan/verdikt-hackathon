"""Deterministic rule-evaluator unit tests.

Locks the contract that the evaluator:

* correctly parses + evaluates the boolean subset it advertises
  (=, is, is true/false, ≤/≥, ∈ {…}, AND-chains, OR-chains).
* correctly detects **undetermined** (referenced field null/absent) so
  the orchestrator's guardrail path doesn't fire on data we do have.
* correctly reports **unparseable** for rules that use arithmetic,
  function calls, prose predicates, nested set conditions, or mixed
  AND/OR without parens — the LLM's answer is what stands for those.

Each test uses a criterion string sampled from the real CardioVita
rule library so a change to the evaluator that would break a live
rule shows up here first.
"""
from __future__ import annotations

from pap_pipeline.rule_evaluator import evaluate


# ---------------------------------------------------------------------------
# Basic single-clause verdicts.
# ---------------------------------------------------------------------------


def test_simple_equality_matched() -> None:
    r = evaluate("@prior_enrollment_active = true", {"prior_enrollment_active": True})
    assert r.verdict == "matched"
    assert len(r.steps) == 1
    assert r.steps[0].result is True


def test_simple_equality_unmatched() -> None:
    r = evaluate("@prior_enrollment_active = true", {"prior_enrollment_active": False})
    assert r.verdict == "unmatched"
    assert r.steps[0].result is False


def test_simple_equality_undetermined_when_missing() -> None:
    r = evaluate("@prior_enrollment_active = true", {})
    assert r.verdict == "undetermined"
    assert r.steps[0].result is None
    assert "undetermined" in r.justification.lower()


def test_string_equality_is_case_insensitive() -> None:
    # Extraction commonly returns human-cased strings; rules use
    # canonical lowercase. Normaliser lets 'Uninsured' match 'uninsured'.
    r = evaluate("@insurance_status = 'uninsured'", {"insurance_status": "Uninsured"})
    assert r.verdict == "matched"


# ---------------------------------------------------------------------------
# Multi-clause AND / OR.
# ---------------------------------------------------------------------------


def test_and_chain_all_true() -> None:
    r = evaluate(
        "@prior_enrollment_active = true AND @renewal_due_within_90d = true",
        {"prior_enrollment_active": True, "renewal_due_within_90d": True},
    )
    assert r.verdict == "matched"
    assert [s.result for s in r.steps] == [True, True]


def test_and_chain_one_false_short_circuits_to_unmatched() -> None:
    r = evaluate(
        "@prior_enrollment_active = true AND @renewal_due_within_90d = true",
        {"prior_enrollment_active": True, "renewal_due_within_90d": False},
    )
    assert r.verdict == "unmatched"


def test_and_chain_missing_field_is_undetermined_not_false() -> None:
    r = evaluate(
        "@prior_enrollment_active = true AND @renewal_due_within_90d = true",
        {"prior_enrollment_active": True},  # renewal_due_within_90d absent
    )
    assert r.verdict == "undetermined"


def test_or_chain_one_true_is_matched() -> None:
    r = evaluate(
        "@ins_uninsured = true OR @has_denial_letter = true",
        {"ins_uninsured": True, "has_denial_letter": False},
    )
    assert r.verdict == "matched"


# ---------------------------------------------------------------------------
# Set membership.
# ---------------------------------------------------------------------------


def test_set_membership_matched() -> None:
    r = evaluate(
        "@insurance_status ∈ {uninsured, medicare, medicaid}",
        {"insurance_status": "uninsured"},
    )
    assert r.verdict == "matched"


def test_set_membership_unmatched() -> None:
    r = evaluate(
        "@insurance_status ∈ {uninsured, medicare, medicaid}",
        {"insurance_status": "commercial"},
    )
    assert r.verdict == "unmatched"


def test_set_membership_with_nested_condition_is_unparseable() -> None:
    # The real CV-INS-01 criterion contains `commercial with @has_denial_letter=true`.
    # Evaluator punts rather than answering wrongly on partial match.
    r = evaluate(
        "@insurance_status ∈ {uninsured, Medicare without Part D, commercial with @has_denial_letter=true}",
        {"insurance_status": "uninsured"},
    )
    assert r.verdict == "unparseable"


# ---------------------------------------------------------------------------
# Numeric comparison.
# ---------------------------------------------------------------------------


def test_le_matched() -> None:
    r = evaluate("@annual_household_income <= 84600", {"annual_household_income": 60000})
    assert r.verdict == "matched"


def test_le_unmatched() -> None:
    r = evaluate("@annual_household_income <= 84600", {"annual_household_income": 100000})
    assert r.verdict == "unmatched"


def test_unicode_le_symbol_supported() -> None:
    r = evaluate("@annual_household_income ≤ 84600", {"annual_household_income": 60000})
    assert r.verdict == "matched"


def test_arithmetic_rhs_is_unparseable() -> None:
    # Real CV-INC-01: `≤ 400% × 2026 FPL(@household_size)` — evaluator
    # doesn't do arithmetic or function calls. Punt.
    r = evaluate(
        "@annual_household_income ≤ 400% × 2026 FPL(@household_size)",
        {"annual_household_income": 60000, "household_size": 2},
    )
    assert r.verdict == "unparseable"


# ---------------------------------------------------------------------------
# Wrapper stripping — "If X, deny" → evaluate X.
# ---------------------------------------------------------------------------


def test_if_deny_wrapper_stripped() -> None:
    r = evaluate("If @prior_enrollment_active = true, deny.", {"prior_enrollment_active": True})
    assert r.verdict == "matched"


def test_if_approve_wrapper_stripped() -> None:
    r = evaluate(
        "If @prior_enrollment_active = true AND @renewal_due_within_90d = true, approve on the renewal track.",
        {"prior_enrollment_active": True, "renewal_due_within_90d": True},
    )
    assert r.verdict == "matched"


# ---------------------------------------------------------------------------
# Unparseable fallthroughs — evaluator stays out of the LLM's way.
# ---------------------------------------------------------------------------


def test_prose_predicate_is_unparseable() -> None:
    # "is a 10-digit number" isn't a recognised operator.
    r = evaluate("@prescriber_npi is a 10-digit number", {"prescriber_npi": "3167245083"})
    assert r.verdict == "unparseable"


def test_mixed_and_or_without_parens_is_unparseable() -> None:
    r = evaluate(
        "@a = 1 AND @b = 2 OR @c = 3",
        {"a": 1, "b": 2, "c": 3},
    )
    assert r.verdict == "unparseable"


def test_empty_criterion_is_unparseable() -> None:
    assert evaluate("", {}).verdict == "unparseable"
    assert evaluate("   ", {}).verdict == "unparseable"


# ---------------------------------------------------------------------------
# Justification prose — feeds directly into the reviewer UI.
# ---------------------------------------------------------------------------


def test_justification_names_field_and_actual_value() -> None:
    r = evaluate("@insurance_status = 'uninsured'", {"insurance_status": "uninsured"})
    assert "@insurance_status" in r.justification
    assert "'uninsured'" in r.justification
    assert "matched" in r.justification.lower()


def test_justification_for_unmatched_carries_actual_value() -> None:
    r = evaluate("@country_of_residence = 'US'", {"country_of_residence": "United States"})
    # 'United States' != 'US' (case-fold normalise, but strings differ).
    assert r.verdict == "unmatched"
    assert "United States".casefold() in r.justification.casefold()
