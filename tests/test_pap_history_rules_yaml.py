"""History-typed YAML rules load through the standard rule loader.

CV-HIS-01 / CV-HIS-02 are the seed history rules referenced by ADR-0004.
They live in ``pap_pipeline/rules/`` alongside the other YAML packs; the
loader must accept ``kind: history`` and expose them under the
``cardiovita-pap-2026`` program filter so retrieval finds them.

A regression here silently drops history rules from the retriever's
candidate set, which downgrades every case with a prior enrollment to the
non-history path — no error, just a wrong outcome. The value of pinning
this test is catching that kind of drift.
"""
from __future__ import annotations

from pap_pipeline.rules import load_rules


def test_cv_his_rules_load_under_cardiovita_program() -> None:
    """Both history rules must appear in the cardiovita rule set with
    kind=history so the reason-stage checklist buckets them separately from
    intake-derived kinds."""
    rules = load_rules("cardiovita-pap-2026")
    by_id = {r.id: r for r in rules}
    assert "CV-HIS-01" in by_id
    assert "CV-HIS-02" in by_id
    assert by_id["CV-HIS-01"].kind == "history"
    assert by_id["CV-HIS-02"].kind == "history"


def test_cv_his_01_references_prior_enrollment_active_slot() -> None:
    """The rule's `fields` must name the history slot the orchestrator
    flattens onto extras. Any rename here must be paired with a rename on
    the simulated MDM adapter's emitted slot names — otherwise the rule
    stops being evaluable."""
    rules = load_rules("cardiovita-pap-2026")
    rule = next(r for r in rules if r.id == "CV-HIS-01")
    assert rule.fields == ["prior_enrollment_active"]


def test_cv_his_02_references_renewal_slot() -> None:
    """Fast-path renewal needs both the active-enrollment flag and the
    renewal-window flag. The simulated MDM emits both for every scenario
    with a well-defined default so this rule can always be evaluated."""
    rules = load_rules("cardiovita-pap-2026")
    rule = next(r for r in rules if r.id == "CV-HIS-02")
    assert "prior_enrollment_active" in rule.fields
    assert "renewal_due_within_90d" in rule.fields
