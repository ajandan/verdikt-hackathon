"""Orchestrator appends history-typed citations for fired slots.

ADR-0004: a rule citation whose ``references_slots`` overlaps a fired slot
in the MDM slice earns one history citation per source event. The reason
stage never emits these — the orchestrator stitches them on after
reasoning, so the ledger's evidence trail names both the rule and the
specific prior event(s) that made it fire.

Regressions here silently break the "which event triggered this deny"
audit trail — the letter still cites the rule, but a reviewer can no
longer trace *why* the rule matched.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pap_pipeline.orchestrator import _attach_history_citations
from pap_pipeline.schemas.decision import Citation, Decision
from pap_pipeline.schemas.history import HistorySlice, HistorySlot
from pap_pipeline.schemas.rule import Rule


def _slice(*slots: HistorySlot, snapshot_hash: str = "sha256:test-hash") -> HistorySlice:
    return HistorySlice(
        member_id="M-1",
        program_family_id="cardiovita",
        events=[],
        slots=list(slots),
        snapshot_hash=snapshot_hash,
        snapshot_at=datetime.now(UTC),
        source_provider="simulated",
    )


def _rule(rule_id: str, *, references: list[str] | None = None) -> Rule:
    return Rule(
        id=rule_id, program="cardiovita", kind="history",
        title="test", criterion="test", exception="",
        fields=references or [],
        references_slots=references,
    )


def _rule_cit(rule_id: str, matched: bool = True) -> Citation:
    return Citation(type="rule", rule_id=rule_id, matched=matched)


def test_appends_history_citation_when_rule_references_fired_slot() -> None:
    """The base case — one rule declares a slot dependency, the slot fired
    on one event, so one history citation gets appended naming the event
    and the snapshot hash."""
    decision = Decision(outcome="deny", citations=[_rule_cit("CV-HIS-01")])
    slice_ = _slice(
        HistorySlot(name="prior_active", value=True, from_events=["E-100"]),
    )
    rules = [_rule("CV-HIS-01", references=["prior_active"])]

    out = _attach_history_citations(decision, slice_, rules)

    history_cits = [c for c in out.citations if c.type == "history"]
    assert len(history_cits) == 1
    assert history_cits[0].event_id == "E-100"
    assert history_cits[0].slot_name == "prior_active"
    assert history_cits[0].snapshot_hash == "sha256:test-hash"


def test_no_citation_for_slots_that_did_not_fire() -> None:
    """A slot with a falsy value has no supporting event to cite. Emitting
    a citation for it would mislead the letter into blaming an absent
    event for the outcome."""
    decision = Decision(outcome="approve", citations=[_rule_cit("CV-HIS-01")])
    slice_ = _slice(
        HistorySlot(name="prior_active", value=False, from_events=[]),
    )
    rules = [_rule("CV-HIS-01", references=["prior_active"])]

    out = _attach_history_citations(decision, slice_, rules)

    assert [c for c in out.citations if c.type == "history"] == []


def test_ignores_rules_without_references_slots() -> None:
    """Rules that don't declare ``references_slots`` aren't history-typed —
    they must never earn a history citation even if a slot fired. This
    preserves the ADR invariant: only rules that opted in reach the
    slot-mediated path."""
    decision = Decision(outcome="approve", citations=[_rule_cit("CV-INC-01")])
    slice_ = _slice(HistorySlot(name="prior_active", value=True, from_events=["E1"]))
    rules = [_rule("CV-INC-01", references=None)]

    out = _attach_history_citations(decision, slice_, rules)

    assert [c for c in out.citations if c.type == "history"] == []


def test_no_citation_when_rule_did_not_match() -> None:
    """A rule that didn't fire against the intake shouldn't drag history
    citations along — the letter cites only rules whose match verdict
    was true, and history citations track those verdicts."""
    decision = Decision(outcome="approve", citations=[_rule_cit("CV-HIS-01", matched=False)])
    slice_ = _slice(HistorySlot(name="prior_active", value=True, from_events=["E1"]))
    rules = [_rule("CV-HIS-01", references=["prior_active"])]

    out = _attach_history_citations(decision, slice_, rules)

    assert [c for c in out.citations if c.type == "history"] == []


def test_dedupes_same_slot_event_across_multiple_rules() -> None:
    """Two rules referencing the same slot must not produce two identical
    history citations for the same source event. Duplicates would inflate
    the evidence list in the letter and break the ledger's replay hash."""
    decision = Decision(
        outcome="deny",
        citations=[_rule_cit("R1"), _rule_cit("R2")],
    )
    slice_ = _slice(HistorySlot(name="s", value=True, from_events=["E1"]))
    rules = [
        _rule("R1", references=["s"]),
        _rule("R2", references=["s"]),
    ]

    out = _attach_history_citations(decision, slice_, rules)

    history_cits = [c for c in out.citations if c.type == "history"]
    assert len(history_cits) == 1


def test_one_citation_per_source_event() -> None:
    """A slot backed by two events cites both. The letter needs both event
    ids so the audit trail can point at each prior determination that
    contributed to the slot being true."""
    decision = Decision(outcome="deny", citations=[_rule_cit("R1")])
    slice_ = _slice(HistorySlot(name="s", value=True, from_events=["E1", "E2"]))
    rules = [_rule("R1", references=["s"])]

    out = _attach_history_citations(decision, slice_, rules)

    history_cits = [c for c in out.citations if c.type == "history"]
    assert {c.event_id for c in history_cits} == {"E1", "E2"}


def test_returns_decision_unchanged_when_slice_has_no_slots() -> None:
    """Empty slice = null MDM path or novel member. The attacher must be
    a no-op so the ledger doesn't grow phantom history entries for
    cases with no history at all."""
    decision = Decision(outcome="approve", citations=[_rule_cit("R1")])
    slice_ = _slice()
    rules = [_rule("R1", references=["s"])]

    out = _attach_history_citations(decision, slice_, rules)

    assert out is decision or [c for c in out.citations if c.type == "history"] == []
