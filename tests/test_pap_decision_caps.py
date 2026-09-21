"""Direct unit tests for the Decision schema's runaway-model caps.

Locks the contract of ``pap_pipeline.schemas.decision._cap_and_flag``:
each of the four Nemotron-authored fields (rationale, citations,
missing_fields, rule_traces) truncates at its documented cap, records
the field name in ``truncation_flags``, and does NOT mutate the
caller's input dict when the cap fires.

Prior to this file the caps + telemetry were exercised only
incidentally by orchestrator tests; a regression that flipped a cap
constant or lost the flag population would have shipped silently.
"""
from __future__ import annotations

from pap_pipeline.schemas.decision import Citation, Decision


def _rule_cit(rule_id: str) -> Citation:
    return Citation(type="rule", rule_id=rule_id, matched=True)


# ---------------------------------------------------------------------------
# Happy path — nothing capped, flags stay empty.
# ---------------------------------------------------------------------------


def test_happy_path_no_flags():
    d = Decision(
        outcome="approve",
        rationale="short rationale",
        citations=[_rule_cit("R-1")],
        missing_fields=["dob"],
        rule_traces={"R-1": {"verdict": "matched"}},
    )
    assert d.truncation_flags == []


# ---------------------------------------------------------------------------
# rationale — 2 KB cap
# ---------------------------------------------------------------------------


def test_rationale_at_boundary_is_not_flagged():
    """Exactly 2048 chars is at the cap, not over it — no flag."""
    d = Decision(outcome="approve", rationale="x" * 2048)
    assert len(d.rationale) == 2048
    assert "rationale" not in d.truncation_flags


def test_rationale_over_boundary_truncates_and_flags():
    d = Decision(outcome="approve", rationale="x" * 3000)
    assert len(d.rationale) == 2048
    assert "rationale" in d.truncation_flags


def test_rationale_none_normalises_to_empty_string():
    """Pydantic tolerates a None from the model; the validator should
    coerce to '' rather than pass None into the str field."""
    d = Decision(outcome="approve", rationale=None)  # type: ignore[arg-type]
    assert d.rationale == ""
    assert "rationale" not in d.truncation_flags


# ---------------------------------------------------------------------------
# citations — 128-item cap
# ---------------------------------------------------------------------------


def test_citations_at_boundary_is_not_flagged():
    cits = [_rule_cit(f"R-{i}") for i in range(128)]
    d = Decision(outcome="approve", citations=cits)
    assert len(d.citations) == 128
    assert "citations" not in d.truncation_flags


def test_citations_over_boundary_truncates_and_flags():
    cits = [_rule_cit(f"R-{i}") for i in range(300)]
    d = Decision(outcome="approve", citations=cits)
    assert len(d.citations) == 128
    assert "citations" in d.truncation_flags
    # Preserves order — first 128 kept.
    assert d.citations[0].rule_id == "R-0"
    assert d.citations[-1].rule_id == "R-127"


# ---------------------------------------------------------------------------
# missing_fields — 128 items × 200 chars each
# ---------------------------------------------------------------------------


def test_missing_fields_count_cap_flags():
    d = Decision(outcome="missing_info", missing_fields=[f"f{i}" for i in range(200)])
    assert len(d.missing_fields) == 128
    assert "missing_fields" in d.truncation_flags


def test_missing_fields_per_entry_length_cap_flags():
    """A single 400-char entry blows the per-entry cap even when the
    list length is within bounds — still a flag."""
    d = Decision(outcome="missing_info", missing_fields=["x" * 400, "ok"])
    assert len(d.missing_fields[0]) == 200
    assert d.missing_fields[1] == "ok"
    assert "missing_fields" in d.truncation_flags


def test_missing_fields_within_all_bounds_no_flag():
    d = Decision(outcome="missing_info", missing_fields=["dob", "insurance_status"])
    assert "missing_fields" not in d.truncation_flags


# ---------------------------------------------------------------------------
# rule_traces — 128-entry dict cap
# ---------------------------------------------------------------------------


def test_rule_traces_at_boundary_is_not_flagged():
    traces = {f"R-{i}": {} for i in range(128)}
    d = Decision(outcome="approve", rule_traces=traces)
    assert len(d.rule_traces) == 128
    assert "rule_traces" not in d.truncation_flags


def test_rule_traces_over_boundary_truncates_and_flags():
    traces = {f"R-{i}": {} for i in range(500)}
    d = Decision(outcome="approve", rule_traces=traces)
    assert len(d.rule_traces) == 128
    assert "rule_traces" in d.truncation_flags


# ---------------------------------------------------------------------------
# Multiple caps in one call — flags list carries every field.
# ---------------------------------------------------------------------------


def test_multiple_caps_flag_together():
    cits = [_rule_cit(f"R-{i}") for i in range(200)]
    d = Decision(
        outcome="approve",
        rationale="x" * 3000,
        citations=cits,
        missing_fields=[f"f{i}" for i in range(300)],
        rule_traces={f"r{i}": {} for i in range(500)},
    )
    assert set(d.truncation_flags) == {
        "rationale", "citations", "missing_fields", "rule_traces",
    }


# ---------------------------------------------------------------------------
# Non-mutation guarantee — the caller's input dict must not be modified.
# ---------------------------------------------------------------------------


def test_input_dict_is_not_mutated_when_caps_fire():
    """Callers passing a shared dict must not see it modified when the
    validator truncates. Regression guard for the round-6 fix that
    switched _cap_and_flag from in-place mutation to shallow-copy."""
    shared: dict[str, object] = {
        "outcome": "approve",
        "rationale": "x" * 3000,
        "missing_fields": [f"f{i}" for i in range(200)],
    }
    Decision(**shared)  # type: ignore[arg-type]
    assert shared["rationale"] == "x" * 3000, "rationale mutated in place"
    assert len(shared["missing_fields"]) == 200, "missing_fields mutated in place"  # type: ignore[arg-type]


def test_input_lists_are_not_mutated_when_caps_fire():
    """Even the nested container references must not be mutated —
    a caller holding the original citations list should still see all
    200 items after construction."""
    cits = [_rule_cit(f"R-{i}") for i in range(200)]
    Decision(outcome="approve", citations=cits)
    assert len(cits) == 200, "citations list mutated in place"


# ---------------------------------------------------------------------------
# truncation_flags is idempotent — replaying a truncated Decision through
# model_validate doesn't append duplicate entries.
# ---------------------------------------------------------------------------


def test_persisted_flags_round_trip_without_duplicates():
    """Once a Decision has been capped and its flags list persisted,
    a subsequent construction from the persisted dict shouldn't add
    duplicate entries (the capped fields are already within cap on the
    second pass, so they can't re-fire)."""
    original = Decision(outcome="approve", rationale="x" * 3000)
    assert original.truncation_flags == ["rationale"]
    replayed = Decision.model_validate(original.model_dump())
    assert replayed.truncation_flags == ["rationale"]
