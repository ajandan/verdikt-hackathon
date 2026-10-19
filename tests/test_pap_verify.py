"""Deterministic verifier (Phase D) + dual-VLM cross-check (Phase C).

Locks the seven consistency invariants in :func:`verify_decision` and the
diff behavior of :func:`dual_extract`. Both are pure Python — no LLM
calls, no HTTP — so tests exercise the logic directly.
"""
from __future__ import annotations

import pytest

from pap_pipeline.schemas.decision import Citation, Decision
from pap_pipeline.schemas.rule import Rule
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.verify import _normalize, _values_agree, dual_extract, verify_decision

# --------------------------------------------------------------------------- #
# verify_decision — the seven invariants
# --------------------------------------------------------------------------- #


def _rule(rid: str, fields: list[str] | None = None) -> Rule:
    """Minimal Rule builder for verifier tests. Kind + strings are fillers."""
    return Rule(
        id=rid,
        program="cardiovita-pap-2026",
        kind="financial",
        title=f"{rid} title",
        criterion="Household income within program cap.",
        fields=fields or [],
    )


def _cite(rule_id: str, matched: bool = True) -> Citation:
    return Citation(type="rule", rule_id=rule_id, matched=matched)


def test_clean_decision_produces_no_findings():
    """Consistent approve verdict — no invariant fires."""
    decision = Decision(
        outcome="approve",
        citations=[_cite("CV-INC-01", matched=True)],
        rationale="All rules satisfied.",
    )
    findings = verify_decision(
        decision,
        intake_dict={"annual_household_income": 54000},
        retrieved_rule_ids=["CV-INC-01"],
        retrieved_rules=[_rule("CV-INC-01", ["annual_household_income"])],
    )
    assert findings == []


def test_hallucinated_citation_is_flagged():
    """Nemotron cited a rule ID that wasn't in the retrieval context."""
    decision = Decision(
        outcome="approve",
        citations=[_cite("CV-INC-99", matched=True)],  # doesn't exist
    )
    findings = verify_decision(
        decision,
        intake_dict={},
        retrieved_rule_ids=["CV-INC-01"],
        retrieved_rules=[_rule("CV-INC-01")],
    )
    assert len(findings) == 1
    assert findings[0].type == "hallucinated_citation"
    assert findings[0].detail["cited_rule_id"] == "CV-INC-99"


def test_missing_info_without_gaps_is_flagged():
    """Outcome says missing_info but the missing_fields list is empty."""
    decision = Decision(
        outcome="missing_info",
        citations=[_cite("CV-INC-01", matched=True)],
        missing_fields=[],
    )
    findings = verify_decision(
        decision,
        intake_dict={},
        retrieved_rule_ids=["CV-INC-01"],
        retrieved_rules=[_rule("CV-INC-01")],
    )
    types = {f.type for f in findings}
    assert "outcome_missing_info_but_no_gaps" in types


def test_missing_field_present_in_intake_is_flagged():
    """Nemotron says a field is missing but the intake already has a value."""
    decision = Decision(
        outcome="missing_info",
        citations=[_cite("CV-INC-01", matched=False)],
        missing_fields=["annual_household_income"],
    )
    findings = verify_decision(
        decision,
        intake_dict={"annual_household_income": 54000},  # not actually missing
        retrieved_rule_ids=["CV-INC-01"],
        retrieved_rules=[_rule("CV-INC-01", ["annual_household_income"])],
    )
    types = {f.type for f in findings}
    assert "outcome_missing_info_gap_not_null_in_intake" in types


def test_missing_field_not_referenced_by_cited_rule():
    """Nemotron asks for a field that no cited rule declares in its `fields`."""
    decision = Decision(
        outcome="missing_info",
        citations=[_cite("CV-INC-01", matched=False)],
        missing_fields=["patient_middle_initial"],  # no rule needs this
    )
    findings = verify_decision(
        decision,
        intake_dict={},
        retrieved_rule_ids=["CV-INC-01"],
        retrieved_rules=[_rule("CV-INC-01", ["annual_household_income"])],
    )
    types = {f.type for f in findings}
    assert "missing_field_not_referenced_by_cited_rule" in types


def test_approve_with_no_matched_citation_is_flagged():
    """Contradiction — approve verdict but every citation shows matched=False."""
    decision = Decision(
        outcome="approve",
        citations=[
            _cite("CV-INC-01", matched=False),
            _cite("CV-INS-01", matched=False),
        ],
    )
    findings = verify_decision(
        decision,
        intake_dict={},
        retrieved_rule_ids=["CV-INC-01", "CV-INS-01"],
        retrieved_rules=[_rule("CV-INC-01"), _rule("CV-INS-01")],
    )
    types = {f.type for f in findings}
    assert "outcome_approve_no_matched_citation" in types


def test_deny_with_all_matched_citations_is_flagged():
    """Contradiction — deny verdict but every cited rule shows matched=True."""
    decision = Decision(
        outcome="deny",
        citations=[
            _cite("CV-INC-01", matched=True),
            _cite("CV-INS-01", matched=True),
        ],
    )
    findings = verify_decision(
        decision,
        intake_dict={},
        retrieved_rule_ids=["CV-INC-01", "CV-INS-01"],
        retrieved_rules=[_rule("CV-INC-01"), _rule("CV-INS-01")],
    )
    types = {f.type for f in findings}
    assert "outcome_deny_no_unmatched_citation" in types


def test_outcome_outside_declared_vocabulary_is_flagged():
    """Program declared {approve, deny} but Nemotron emitted missing_info."""
    decision = Decision(
        outcome="missing_info",
        citations=[_cite("CV-INC-01", matched=False)],
        missing_fields=["annual_household_income"],
    )
    findings = verify_decision(
        decision,
        intake_dict={},
        retrieved_rule_ids=["CV-INC-01"],
        retrieved_rules=[_rule("CV-INC-01", ["annual_household_income"])],
        declared_outcomes=["approve", "deny"],  # missing_info NOT allowed
    )
    types = {f.type for f in findings}
    assert "outcome_outside_declared_vocabulary" in types


# --------------------------------------------------------------------------- #
# dual_extract — diff behavior
# --------------------------------------------------------------------------- #


def _cfg() -> ExtractClientConfig:
    return ExtractClientConfig(base_url="https://x", api_key="k")


@pytest.mark.asyncio
async def test_dual_extract_no_findings_when_extractors_agree(monkeypatch):
    """Both extractors return identical dicts → zero findings."""
    calls: list[str] = []

    async def _fake_extract(attachments, *, config, slots=None):
        calls.append(config.vision_model or "primary")
        return {"annual_household_income": 54000, "patient_name": "Devon"}

    monkeypatch.setattr("pap_pipeline.verify.extract", _fake_extract)
    raw, findings = await dual_extract(
        [b"pdf"],
        primary_config=_cfg(),
        verifier_config=_cfg(),
    )
    assert raw["annual_household_income"] == 54000
    assert findings == []
    assert len(calls) == 2  # both extractors ran


@pytest.mark.asyncio
async def test_dual_extract_flags_field_disagreement(monkeypatch):
    """Extractors disagree on income — one critical finding emitted."""
    async def _fake_extract(attachments, *, config, slots=None):
        # Primary sees $54k, verifier sees $70k
        if config.vision_model == "verifier":
            return {"annual_household_income": 70000, "patient_name": "Devon"}
        return {"annual_household_income": 54000, "patient_name": "Devon"}

    monkeypatch.setattr("pap_pipeline.verify.extract", _fake_extract)
    primary = ExtractClientConfig(base_url="x", api_key="k", vision_model="primary")
    verifier = ExtractClientConfig(base_url="x", api_key="k", vision_model="verifier")
    raw, findings = await dual_extract([b"pdf"], primary_config=primary, verifier_config=verifier)
    assert raw["annual_household_income"] == 54000  # primary's value returned
    assert len(findings) == 1
    assert findings[0].type == "extract_disagreement"
    assert findings[0].detail["field"] == "annual_household_income"
    assert findings[0].detail["primary"] == 54000
    assert findings[0].detail["verifier"] == 70000


@pytest.mark.asyncio
async def test_dual_extract_ignores_null_vs_value(monkeypatch):
    """One extractor found a value, the other returned null — treated as
    extraction gap (not a disagreement). No finding emitted."""
    async def _fake_extract(attachments, *, config, slots=None):
        if config.vision_model == "verifier":
            return {"annual_household_income": None}  # gap on verifier
        return {"annual_household_income": 54000}

    monkeypatch.setattr("pap_pipeline.verify.extract", _fake_extract)
    primary = ExtractClientConfig(base_url="x", api_key="k", vision_model="primary")
    verifier = ExtractClientConfig(base_url="x", api_key="k", vision_model="verifier")
    _, findings = await dual_extract([b"pdf"], primary_config=primary, verifier_config=verifier)
    assert findings == []


@pytest.mark.asyncio
async def test_dual_extract_absorbs_verifier_failure(monkeypatch):
    """Verifier throws — pipeline continues, warning finding emitted."""
    async def _fake_extract(attachments, *, config, slots=None):
        if config.vision_model == "verifier":
            raise RuntimeError("connection reset")
        return {"annual_household_income": 54000}

    monkeypatch.setattr("pap_pipeline.verify.extract", _fake_extract)
    primary = ExtractClientConfig(base_url="x", api_key="k", vision_model="primary")
    verifier = ExtractClientConfig(base_url="x", api_key="k", vision_model="verifier")
    raw, findings = await dual_extract([b"pdf"], primary_config=primary, verifier_config=verifier)
    assert raw["annual_household_income"] == 54000  # primary still shipped
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].detail["reason"] == "verifier_call_failed"


# --------------------------------------------------------------------------- #
# _normalize — noise-tolerant comparison
# --------------------------------------------------------------------------- #


def test_normalize_treats_stringified_numbers_as_numbers():
    """"60000" and 60000 shouldn't count as different values."""
    assert _normalize("60000") == _normalize(60000)


def test_normalize_strips_and_lowercases_strings():
    assert _normalize("  Devon Okafor  ") == _normalize("devon okafor")


# --------------------------------------------------------------------------- #
# _values_agree — formatting differences between two VLMs are not conflicts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("a,b", [
    ("30", "30 tablets"),                 # seen 2026-10-18: MiniCPM-V vs Nemotron VL
    (30, "30 tablets"),
    ("2.5", "2.5 mg"),
    ("Chen Mei", "Chen Mei L"),           # seen on the synthetic intake fixture
    ("Mei L. Chen", "Chen Mei"),
    ("Devon Okafor", "okafor devon"),
])
def test_same_reading_written_differently_agrees(a, b):
    assert _values_agree(a, b) and _values_agree(b, a)


@pytest.mark.parametrize("a,b", [
    ("1990-07-07", "1940-07-07"),         # the wrong-decade DOB the verifier exists to catch
    ("1990-03-07", "1990-07-03"),         # same parts, different date
    ("30", "40 tablets"),
    ("30 mg", "30 tablets"),
    ("Y", "N"),
    ("Type A", "Type B"),
    ("John A Smith", "John B Smith"),     # conflicting initials
    ("Aldurizumab", "Adliruzumab"),
    ("Mei Chen", "Mei Chan"),
])
def test_real_disagreements_still_disagree(a, b):
    assert not _values_agree(a, b) and not _values_agree(b, a)


@pytest.mark.asyncio
async def test_dual_extract_ignores_unit_and_initial_differences(monkeypatch):
    async def _fake_extract(attachments, *, config, slots=None):
        if config.vision_model == "verifier":
            return {"rx_quantity": "30 tablets", "patient_name": "Chen Mei L", "dob": "1990-07-07"}
        return {"rx_quantity": "30", "patient_name": "Chen Mei", "dob": "1940-07-07"}

    monkeypatch.setattr("pap_pipeline.verify.extract", _fake_extract)
    primary = ExtractClientConfig(base_url="x", api_key="k", vision_model="primary")
    verifier = ExtractClientConfig(base_url="x", api_key="k", vision_model="verifier")
    _, findings = await dual_extract([b"pdf"], primary_config=primary, verifier_config=verifier)
    assert [f.detail["field"] for f in findings] == ["dob"]
