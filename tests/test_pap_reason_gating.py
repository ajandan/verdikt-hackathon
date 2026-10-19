"""Integration test: runtime Reason-stage gating end-to-end.

When every cited rule carries a structured formula, the orchestrator
skips the Nemotron Reason call entirely and synthesises the Decision
from deterministic formula evaluations. This test locks that
behaviour by:

1. Running ``run_from_intake`` with a QC-81-shape intake + all 7
   CardioVita rules formula-backed.
2. Asserting the outcome + rationale match what the composer produces
   from citations + polarity — no LLM in the loop.
3. Verifying zero HTTP calls to the reason endpoint (the whole
   AsyncClient is monkeypatched to record request URLs; asserts no
   ``/chat/completions`` request fired).

Corollary test in the "mixed" mode (some rules with formula, some
without) confirms the LLM IS called in that case, then the evaluator
still overrides per-rule verdicts. Locks the gate's semantics.
"""
from __future__ import annotations

import json

import httpx
import pytest

from pap_pipeline.orchestrator import (
    PipelineConfig,
    run_from_intake,
)
from pap_pipeline.rules import Rule as PipelineRule
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.stages.mdm_lookup import MdmSourceConfig, build_mdm_source
from pap_pipeline.stages.reason import ReasonClientConfig
from pap_pipeline.stages.retrieve import RetrieveClientConfig


def _config() -> PipelineConfig:
    return PipelineConfig(
        extract=ExtractClientConfig(
            base_url="https://api.example.test/v1",
            api_key="test-key",
            retry_null_on_scan=False,
        ),
        retrieve=RetrieveClientConfig(
            base_url="https://api.example.test/v1",
            api_key="test-key",
            rerank_model=None,  # skip rerank; use embed cosine
        ),
        reason=ReasonClientConfig(
            base_url="https://api.example.test/v1",
            api_key="test-key",
            model="test-nemotron",
        ),
        mdm=MdmSourceConfig(provider="null"),
    )


def _mock_transport(captured_urls: list[str]):
    """Route every AsyncClient call through a MockTransport that records
    URLs + serves a deterministic embed response. Reason endpoint fails
    loudly if it's ever hit (the gate should skip it)."""
    def _handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        captured_urls.append(url)
        if url.endswith("/embeddings"):
            # Return equal unit vectors so all rules survive retrieval
            # in the fixed order they arrived — cosine is uniform.
            n = len(json.loads(req.content)["input"])
            return httpx.Response(200, json={
                "data": [{"embedding": [1.0, 0.0, 0.0]} for _ in range(n)],
            })
        if "/chat/completions" in url:
            # The Reason endpoint should never be hit when all rules
            # have formulas. If we ever see this call the test fails
            # visibly rather than silently exercising the LLM path.
            return httpx.Response(500, json={"error": "reason endpoint should be skipped"})
        return httpx.Response(404)
    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_reason_skipped_when_all_rules_have_formula(monkeypatch) -> None:
    """QC-81 shape end-to-end via formula-only path.

    Applicant: John Smith / Metformin / has BCBS / signed Jan 2024 /
    didn't check req_pap. Expected composed outcome: deny (multiple
    approve gates fail). No LLM Reason call fires."""
    urls: list[str] = []

    real_async = httpx.AsyncClient
    def _factory(*args, **kwargs):
        kwargs["transport"] = _mock_transport(urls)
        return real_async(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    # QC-81 intake (relevant fields only)
    intake = Intake()  # canonical Intake left mostly blank
    extras = {
        "ins_uninsured": False,
        "rx_medication": "Metformin",
        "prescriber_npi": "1234567890",
        "rx_date_written": "2024-01-15",
        "patient_state": "CA",
        "practice_state": "CA",
        "patient_signature_date": "2024-01-15",
        "prescriber_signature_date": "2024-01-15",
        "req_pap": False,
    }

    # Same 7 rules the live DB carries, all formula-backed.
    rules = [
        PipelineRule(id="CV-INS-01-cardio", program="cardiovita-pap-2026", kind="insurance",
                     title="Uninsured", criterion="@ins_uninsured = true", fields=["ins_uninsured"],
                     on_match="approve",
                     formula={"op": "==", "field": "ins_uninsured", "value": True}),
        PipelineRule(id="CV-CLIN-01-cardio", program="cardiovita-pap-2026", kind="clinical",
                     title="Rx", criterion="...", fields=["rx_medication", "prescriber_npi", "rx_date_written"],
                     on_match="approve",
                     formula={"op": "AND", "terms": [
                         {"op": "==", "field": "rx_medication", "value": "Cardiovita"},
                         {"op": "digit_count_equals", "field": "prescriber_npi", "n": 10},
                         {"op": "within_days", "field": "rx_date_written", "n": 365},
                     ]}),
        PipelineRule(id="CV-RES-01-cardio", program="cardiovita-pap-2026", kind="residency",
                     title="Patient US state", criterion="...", fields=["patient_state"],
                     on_match="approve",
                     formula={"op": "in", "field": "patient_state",
                              "values": ["AL","AK","AZ","CA","NY","TX","WA"]}),
        PipelineRule(id="CV-PRACTICE-US-01", program="cardiovita-pap-2026", kind="residency",
                     title="Practice US state", criterion="...", fields=["practice_state"],
                     on_match="approve",
                     formula={"op": "in", "field": "practice_state",
                              "values": ["AL","AK","AZ","CA","NY","TX","WA"]}),
        PipelineRule(id="CV-CONSENT-01", program="cardiovita-pap-2026", kind="clinical",
                     title="Patient consent", criterion="...", fields=["patient_signature_date"],
                     on_match="approve",
                     formula={"op": "within_days", "field": "patient_signature_date", "n": 90}),
        PipelineRule(id="CV-PRESCRIBER-CONSENT-01", program="cardiovita-pap-2026", kind="clinical",
                     title="Prescriber consent", criterion="...", fields=["prescriber_signature_date"],
                     on_match="approve",
                     formula={"op": "within_days", "field": "prescriber_signature_date", "n": 90}),
        PipelineRule(id="CV-PAP-REQUEST-01", program="cardiovita-pap-2026", kind="financial",
                     title="PAP request", criterion="...", fields=["req_pap"],
                     on_match="approve",
                     formula={"op": "==", "field": "req_pap", "value": True}),
    ]

    decision = await run_from_intake(
        intake,
        config=_config(),
        program="cardiovita-pap-2026",
        extras=extras,
        rules=rules,
        k=20,
    )

    # No /chat/completions request should have been made — the gate
    # skips Reason when every rule has a formula.
    assert not any("/chat/completions" in u for u in urls), (
        f"Reason endpoint was hit despite full formula coverage: {urls}"
    )

    # Composer output: deny because 5 approve gates failed
    # (ins_uninsured false, wrong drug, both signatures too old,
    # req_pap false). CV-RES-01-cardio + CV-PRACTICE-US-01 pass.
    assert decision.outcome == "deny", f"expected deny, got {decision.outcome} ({decision.rationale})"
    rule_cites = [c for c in decision.citations if c.type == "rule"]
    assert len(rule_cites) == 7
    verdicts = {c.rule_id: c.matched for c in rule_cites}
    assert verdicts["CV-INS-01-cardio"] is False        # not uninsured
    assert verdicts["CV-CLIN-01-cardio"] is False       # wrong drug
    assert verdicts["CV-RES-01-cardio"] is True         # CA in set
    assert verdicts["CV-PRACTICE-US-01"] is True        # CA in set
    assert verdicts["CV-CONSENT-01"] is False           # signature stale
    assert verdicts["CV-PRESCRIBER-CONSENT-01"] is False  # ditto
    assert verdicts["CV-PAP-REQUEST-01"] is False       # unchecked


@pytest.mark.asyncio
async def test_reason_called_when_any_rule_lacks_formula(monkeypatch) -> None:
    """Mixed mode: at least one rule has no formula → LLM must run.

    Verifies the gate's precondition ("ALL rules have formula") and
    not "ANY rule has formula". A single formula-less rule keeps the
    LLM in the loop for correctness on the un-covered criterion."""
    urls: list[str] = []
    def _handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        urls.append(url)
        if url.endswith("/embeddings"):
            n = len(json.loads(req.content)["input"])
            return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0, 0.0]} for _ in range(n)]})
        if "/chat/completions" in url:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "outcome": "approve",
                "citations": [{"rule_id": "R-FORMULA", "matched": True, "score": None},
                              {"rule_id": "R-TEXT", "matched": True, "score": None}],
                "missing_fields": [],
                "rationale": "LLM decided all matched.",
            })}}]})
        return httpx.Response(404)

    real_async = httpx.AsyncClient
    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    rules = [
        PipelineRule(id="R-FORMULA", program="p", kind="clinical", title="A",
                     criterion="@x = true", fields=["x"], on_match="approve",
                     formula={"op": "==", "field": "x", "value": True}),
        PipelineRule(id="R-TEXT", program="p", kind="clinical", title="B",
                     criterion="something prose the evaluator can't parse", fields=["y"],
                     on_match="approve",
                     formula=None),   # <-- no formula
    ]
    decision = await run_from_intake(
        Intake(),
        config=_config(),
        program="p",
        extras={"x": True, "y": "anything"},
        rules=rules,
        k=20,
    )
    assert any("/chat/completions" in u for u in urls), \
        f"Reason endpoint was NOT hit despite mixed formula coverage: {urls}"
    # LLM said approve; composer + evaluator hybrid runs as before.
    assert decision.outcome == "approve"
