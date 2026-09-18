"""Composite-outcome derivation tests.

Locks the contract that:

* The deterministic rule evaluator's verdict OVERRIDES the reasoner's
  ``citation.matched`` when they disagree — the LLM's original value is
  preserved on the trace + finding but downstream systems see the
  mechanically-verified truth.
* Bogus ``missing_fields`` entries (fields not declared on any cited
  rule) are stripped so applicant reply letters and JSON verdicts
  don't propagate hallucinated field asks.
* The outcome is derived mechanically from citations + rule polarity
  when every cited rule has a decisive verdict on the trace. State
  machine: approve_shortcircuit matched → approve; deny matched →
  deny; approve unmatched → deny; else approve.
* Unparseable rules (evaluator punted) leave the LLM's outcome in
  charge — the composer never overrides based on incomplete truth.

Uses the shape of QC-0069 as the canary integration case: 7 rules,
CV-HIS-02 was the fast-path renewal that Nemotron missed; the composer
should re-derive approve.
"""
from __future__ import annotations

from typing import Any

import pytest

from pap_pipeline.orchestrator import (
    _compose_outcome_from_polarity,
    _strip_bogus_missing_fields,
)
from pap_pipeline.schemas.decision import Citation, Decision
from pap_pipeline.schemas.rule import Rule


def _rule(rid: str, on_match: str = "approve", fields: list[str] | None = None, criterion: str = "@x = true") -> Rule:
    return Rule(
        id=rid,
        program="cv-2026",
        kind="clinical",
        title=rid,
        criterion=criterion,
        exception="",
        fields=fields or ["x"],
        on_match=on_match,
    )


def _decision(
    *,
    outcome: str = "missing_info",
    citations: list[Citation] | None = None,
    missing_fields: list[str] | None = None,
    rule_traces: dict[str, dict[str, Any]] | None = None,
) -> Decision:
    return Decision(
        outcome=outcome,
        citations=citations or [],
        missing_fields=missing_fields or [],
        rationale="test",
        rule_traces=rule_traces or {},
    )


def _cite(rule_id: str, matched: bool) -> Citation:
    return Citation(type="rule", rule_id=rule_id, matched=matched)


def _trace(verdict: str) -> dict[str, Any]:
    return {"verdict": verdict, "steps": [], "justification": "", "agrees_with_reason": True}


# ---------------------------------------------------------------------------
# Bogus missing_fields stripping
# ---------------------------------------------------------------------------


def test_strip_removes_fields_not_declared_by_any_cited_rule() -> None:
    rules = [_rule("A", fields=["real_field"])]
    d = _decision(
        outcome="missing_info",
        citations=[_cite("A", False)],
        missing_fields=["real_field", "hallucinated_field"],
    )
    out = _strip_bogus_missing_fields(d, rules)
    assert out.missing_fields == ["real_field"]


def test_strip_leaves_valid_missing_fields_intact() -> None:
    rules = [_rule("A", fields=["real_field"])]
    d = _decision(
        outcome="missing_info",
        citations=[_cite("A", False)],
        missing_fields=["real_field"],
    )
    out = _strip_bogus_missing_fields(d, rules)
    assert out.missing_fields == ["real_field"]
    assert out is d  # no mutation when no strip


def test_strip_no_op_when_no_missing_fields() -> None:
    rules = [_rule("A")]
    d = _decision(citations=[_cite("A", True)])
    assert _strip_bogus_missing_fields(d, rules) is d


# ---------------------------------------------------------------------------
# Composite-outcome derivation — the four state-machine branches
# ---------------------------------------------------------------------------


def test_shortcircuit_matched_forces_approve() -> None:
    # Fast-path override wins even if approve gates would have failed.
    rules = [
        _rule("SC", on_match="approve_shortcircuit"),
        _rule("GATE", on_match="approve"),
    ]
    d = _decision(
        outcome="missing_info",
        citations=[_cite("SC", True), _cite("GATE", False)],
        rule_traces={"SC": _trace("matched"), "GATE": _trace("unmatched")},
    )
    out = _compose_outcome_from_polarity(d, rules)
    assert out.outcome == "approve"
    assert "SC" in out.rationale
    assert out.missing_fields == []


def test_deny_gate_matched_forces_deny() -> None:
    rules = [
        _rule("DENY", on_match="deny"),
        _rule("GATE", on_match="approve"),
    ]
    d = _decision(
        outcome="approve",
        citations=[_cite("DENY", True), _cite("GATE", True)],
        rule_traces={"DENY": _trace("matched"), "GATE": _trace("matched")},
    )
    out = _compose_outcome_from_polarity(d, rules)
    assert out.outcome == "deny"
    assert "DENY" in out.rationale


def test_failed_approve_gate_forces_deny() -> None:
    rules = [
        _rule("GATE1", on_match="approve"),
        _rule("GATE2", on_match="approve"),
    ]
    d = _decision(
        outcome="approve",
        citations=[_cite("GATE1", True), _cite("GATE2", False)],
        rule_traces={"GATE1": _trace("matched"), "GATE2": _trace("unmatched")},
    )
    out = _compose_outcome_from_polarity(d, rules)
    assert out.outcome == "deny"
    assert "GATE2" in out.rationale


def test_all_approve_gates_matched_no_deny_fires_approves() -> None:
    rules = [
        _rule("GATE1", on_match="approve"),
        _rule("GATE2", on_match="approve"),
        _rule("BLK", on_match="deny"),
    ]
    d = _decision(
        outcome="missing_info",
        citations=[_cite("GATE1", True), _cite("GATE2", True), _cite("BLK", False)],
        rule_traces={
            "GATE1": _trace("matched"),
            "GATE2": _trace("matched"),
            "BLK": _trace("unmatched"),
        },
    )
    out = _compose_outcome_from_polarity(d, rules)
    assert out.outcome == "approve"


def test_undetermined_rule_forces_missing_info() -> None:
    rules = [_rule("A", on_match="approve", fields=["a"])]
    d = _decision(
        outcome="approve",
        citations=[_cite("A", True)],
        rule_traces={"A": _trace("undetermined")},
    )
    out = _compose_outcome_from_polarity(d, rules)
    assert out.outcome == "missing_info"


def test_unparseable_rule_leaves_llm_outcome_alone() -> None:
    # Evaluator punted — no mechanical verdict to trust. LLM stays.
    rules = [_rule("A")]
    d = _decision(
        outcome="deny",  # LLM said deny
        citations=[_cite("A", False)],
        rule_traces={"A": _trace("unparseable")},
    )
    out = _compose_outcome_from_polarity(d, rules)
    assert out.outcome == "deny"
    assert out is d


def test_missing_trace_leaves_llm_outcome_alone() -> None:
    rules = [_rule("A")]
    d = _decision(outcome="deny", citations=[_cite("A", False)], rule_traces={})
    out = _compose_outcome_from_polarity(d, rules)
    assert out is d


# ---------------------------------------------------------------------------
# QC-0069 replay — the case that motivated the whole polarity project.
# ---------------------------------------------------------------------------


def test_qc0069_replay_composes_to_approve_via_fast_path() -> None:
    """QC-0069 shape: renewal case with prior_enrollment_active=true AND
    renewal_due_within_90d=true. CV-HIS-02 is the fast-path
    (approve_shortcircuit). Nemotron returned matched=false + outcome
    missing_info — the composer + evaluator override should flip both."""
    rules = [
        _rule("CV-HIS-02", on_match="approve_shortcircuit",
              fields=["prior_enrollment_active", "renewal_due_within_90d"],
              criterion="@prior_enrollment_active = true AND @renewal_due_within_90d = true"),
        _rule("CV-HIS-01", on_match="deny",
              fields=["prior_enrollment_active"],
              criterion="@prior_enrollment_active = true"),
        _rule("CV-INS-01", on_match="approve", fields=["insurance_status"]),
        _rule("CV-INC-01", on_match="approve",
              fields=["household_size", "annual_household_income"]),
        _rule("CV-RES-01", on_match="approve",
              fields=["country_of_residence", "mailing_state"]),
        _rule("CV-CLIN-01", on_match="approve", fields=["prescribed_drug"]),
        _rule("CV-INS-01-cardiovita-pap-2", on_match="approve", fields=["ins_uninsured"]),
    ]
    # After evaluator override: CV-HIS-02 matched, CV-HIS-01 matched
    # (prior_enrollment_active=true means "deny" rule fired). Fast-path
    # trumps deny in the state machine, so overall outcome = approve.
    d = _decision(
        outcome="missing_info",
        citations=[
            _cite("CV-HIS-02", True),      # was false in QC-0069; override flipped it
            _cite("CV-HIS-01", True),      # prior_enrollment_active true → deny gate fires
            _cite("CV-INS-01", False),
            _cite("CV-INC-01", False),
            _cite("CV-RES-01", False),
            _cite("CV-CLIN-01", False),
            _cite("CV-INS-01-cardiovita-pap-2", False),
        ],
        # Only two rules parse cleanly; others are unparseable in the
        # evaluator's current grammar. All UNPARSEABLE traces would
        # short-circuit to "LLM authoritative" — so for the composer to
        # kick in we need every trace to be decisive.
        rule_traces={
            "CV-HIS-02": _trace("matched"),
            "CV-HIS-01": _trace("matched"),
            "CV-INS-01": _trace("unmatched"),
            "CV-INC-01": _trace("unmatched"),
            "CV-RES-01": _trace("unmatched"),
            "CV-CLIN-01": _trace("unmatched"),
            "CV-INS-01-cardiovita-pap-2": _trace("unmatched"),
        },
    )
    out = _compose_outcome_from_polarity(d, rules)
    # Fast-path fires FIRST in the state machine, so despite the deny
    # gate + failed approve gates, CV-HIS-02 wins → approve.
    assert out.outcome == "approve", (
        f"expected approve (CV-HIS-02 fast-path), got {out.outcome}"
    )
    assert "CV-HIS-02" in out.rationale
