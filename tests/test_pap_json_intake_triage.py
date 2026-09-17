"""Regression guard: the JSON intake channel must run Triage.

The bug being pinned: `pap_pipeline.api_hook.run_json_intake_sync` used to
call `orchestrator.run_from_intake` directly — which composes retrieve +
reason but NOT triage. Every JSON-in case therefore had
`decision.triage = None`, and the entire tenant/program Triage-routing
config was dead code on the JSON channel.

Fix landed 2026-09-17: introduced `orchestrator.run_triaged_from_intake`
as the shared middle-slice and rewired both the inbox channel and the
JSON channel through it.

This test pins the wire behavior so a future refactor can't quietly
undo the fix. Two lenses:

1. **Import-level** — api_hook must reference the triaged entrypoint,
   not the bare `run_from_intake`. Fast, cheap, catches a name-only
   regression before it ships.
2. **Functional** — driving `run_triaged_from_intake` end-to-end with
   patched httpx must produce a Decision whose `triage` field is
   populated with the case_type and route the classifier picked.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pap_pipeline import api_hook
from pap_pipeline.orchestrator import (
    PipelineConfig,
    run_triaged_from_intake,
)
from pap_pipeline.schemas.intake import Intake
from pap_pipeline.stages import extract as extract_mod
from pap_pipeline.stages import reason as reason_mod
from pap_pipeline.stages import retrieve as retrieve_mod
from pap_pipeline.stages import triage as triage_mod
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.stages.reason import ReasonClientConfig
from pap_pipeline.stages.retrieve import RetrieveClientConfig
from pap_pipeline.stages.triage import TriageConfig


# ---------------------------------------------------------------------------
# Lens 1 — import-level regression guard
# ---------------------------------------------------------------------------

def test_api_hook_imports_run_triaged_from_intake() -> None:
    """The JSON channel entrypoint must reference the triaged variant.

    A grep-style check on the module's own symbol table — cheap, and
    catches the exact class of regression that motivated this test
    (someone reverts the import back to `run_from_intake`).
    """
    assert hasattr(api_hook, "run_triaged_from_intake"), (
        "api_hook must import run_triaged_from_intake — the JSON intake "
        "channel bypasses Triage without it. See orchestrator.py + fix "
        "commit for context."
    )
    assert not hasattr(api_hook, "run_from_intake") or (
        api_hook.run_from_intake is not api_hook.run_triaged_from_intake
    ), (
        "If api_hook also imports run_from_intake, ensure it's the untriaged "
        "variant is not the one wired into run_json_intake_sync."
    )


# ---------------------------------------------------------------------------
# Lens 2 — functional wire-capture on the shared middle-slice
# ---------------------------------------------------------------------------

def _config() -> PipelineConfig:
    """PipelineConfig with Triage enabled — mirrors what inbox_hook /
    api_hook build via `_pipeline_config`. Fake sentinel URLs so a
    missing MockTransport path becomes a loud connection refusal, not
    a real Nebius hit."""
    return PipelineConfig(
        extract=ExtractClientConfig(
            base_url="http://test-nebius.local/v1",
            api_key="test-key",
            vision_model="TEST-EXTRACT",
        ),
        retrieve=RetrieveClientConfig(
            base_url="http://test-nebius.local/v1",
            api_key="test-key",
            embed_model="TEST-EMBED",
        ),
        reason=ReasonClientConfig(
            base_url="http://test-nebius.local/v1",
            api_key="test-key",
            model="TEST-REASON",
        ),
        triage=TriageConfig(
            enabled=True,
            base_url="http://test-nebius.local/v1",
            api_key="test-key",
            model="TEST-TRIAGE",
        ),
    )


def _patch_stages(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    for mod in (extract_mod, retrieve_mod, reason_mod, triage_mod):
        monkeypatch.setattr(mod.httpx, "AsyncClient", _factory)


def _stub_intake() -> Intake:
    """Minimal Intake — the JSON channel would build this from the
    validated payload; the test builds it directly to skip the DB seeding
    the api_hook itself needs. What we're pinning is downstream of that
    boundary."""
    return Intake(
        patient_name="Devon Okafor",
        family_name="Okafor",
        prescribed_drug="Cardiovita",
        insurance_status="uninsured",
        household_size=2,
        annual_household_income=40000.00,
        country_of_residence="United States",
        mailing_state="California",
    )


@pytest.mark.asyncio
async def test_run_triaged_from_intake_populates_triage_on_json_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared middle-slice must classify and stamp decision.triage.

    Prior to the fix, the JSON channel called run_from_intake which
    skipped this step entirely — decision.triage stayed None and none
    of the tenant/program triage configuration reached the wire.
    """
    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = json.loads(request.content) if request.content else {}
        if p.endswith("/chat/completions"):
            first = body.get("messages", [{}])[0]
            system = " ".join(
                str(m.get("content", ""))
                for m in body.get("messages", [])
                if m.get("role") == "system"
            )
            if "case-triage classifier" in system.lower():
                # Triage classifier response
                return httpx.Response(200, json={"choices": [
                    {"message": {"content": json.dumps({
                        "case_type": "new_intake",
                        "confidence": 0.95,
                        "reason": "stub",
                    })}}
                ]})
            # Reason stage
            return httpx.Response(200, json={"choices": [
                {"message": {"content": json.dumps({
                    "outcome": "approve",
                    "citations": [],
                    "missing_fields": [],
                    "rationale": "e2e stub",
                })}}
            ]})
        if p.endswith("/embeddings"):
            n = len(body.get("input", []))
            return httpx.Response(200, json={"data": [
                {"embedding": [1.0 if i == j else 0.0 for j in range(max(n, 2))][:max(n, 2)]}
                for i in range(n)
            ]})
        if p.endswith("/rerank"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)

    _patch_stages(monkeypatch, _handler)

    decision = await run_triaged_from_intake(
        _stub_intake(),
        config=_config(),
        program="cardiovita-pap-2026",
        tenant_id="test-tenant",
        rules=[],
    )

    # Core regression assertion: triage MUST be populated on the JSON path.
    assert decision.triage is not None, (
        "decision.triage is None — Triage stage did not run. This is the "
        "regression that motivated this test."
    )
    assert decision.triage.case_type == "new_intake"
    # Route resolves via the platform default routing table for new_intake.
    assert decision.triage.route == "decide"
    # decision_needed=True for new_intake → the Decide path ran too.
    assert decision.triage.decision_needed is True
    assert decision.outcome == "approve"


@pytest.mark.asyncio
async def test_run_triaged_from_intake_skips_decide_when_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Triage returns decision_needed=False (e.g. refill), the
    middle-slice must emit a stub Decision without invoking Reason.

    This test also proves the JSON channel now honors per-tenant
    routing overrides that flip decision_needed off — before the fix,
    such overrides were dead code because Triage never ran here.
    """
    reason_called = [False]

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = json.loads(request.content) if request.content else {}
        if p.endswith("/chat/completions"):
            system = " ".join(
                str(m.get("content", ""))
                for m in body.get("messages", [])
                if m.get("role") == "system"
            )
            if "case-triage classifier" in system.lower():
                # Classify as refill — platform default routes refill to
                # pharmacy_direct with decision_needed=False.
                return httpx.Response(200, json={"choices": [
                    {"message": {"content": json.dumps({
                        "case_type": "refill",
                        "confidence": 0.99,
                        "reason": "prior_prescription_id present",
                    })}}
                ]})
            reason_called[0] = True
            return httpx.Response(200, json={"choices": [
                {"message": {"content": json.dumps({
                    "outcome": "approve",
                    "citations": [],
                    "missing_fields": [],
                    "rationale": "should not have been called",
                })}}
            ]})
        if p.endswith("/embeddings") or p.endswith("/rerank"):
            return httpx.Response(200, json={"data": [], "results": []})
        return httpx.Response(404)

    _patch_stages(monkeypatch, _handler)

    decision = await run_triaged_from_intake(
        _stub_intake(),
        config=_config(),
        program="cardiovita-pap-2026",
        tenant_id="test-tenant",
        rules=[],
        subject="Refill request",
    )

    assert reason_called[0] is False, (
        "Reason stage fired despite decision_needed=False. The skip-Decide "
        "branch is broken on the shared middle-slice."
    )
    assert decision.triage is not None
    assert decision.triage.case_type == "refill"
    assert decision.triage.route == "pharmacy_direct"
    assert decision.triage.decision_needed is False
    assert "Decide skipped" in (decision.rationale or "")


@pytest.mark.asyncio
async def test_run_triaged_from_intake_no_triage_preserves_pre_triage_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`config.triage=None` must skip Triage entirely and behave exactly
    like the pre-Triage `run_from_intake` — callers that haven't wired
    Triage yet keep working."""
    triage_called = [False]

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = json.loads(request.content) if request.content else {}
        if p.endswith("/chat/completions"):
            system = " ".join(
                str(m.get("content", ""))
                for m in body.get("messages", [])
                if m.get("role") == "system"
            )
            if "case-triage classifier" in system.lower():
                triage_called[0] = True
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"choices": [
                {"message": {"content": json.dumps({
                    "outcome": "approve",
                    "citations": [],
                    "missing_fields": [],
                    "rationale": "no-triage path",
                })}}
            ]})
        if p.endswith("/embeddings"):
            n = len(body.get("input", []))
            return httpx.Response(200, json={"data": [
                {"embedding": [1.0 if i == j else 0.0 for j in range(max(n, 2))][:max(n, 2)]}
                for i in range(n)
            ]})
        if p.endswith("/rerank"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)

    _patch_stages(monkeypatch, _handler)

    # PipelineConfig is a frozen dataclass — rebuild it with triage=None
    # instead of mutating the fixture in place.
    import dataclasses
    cfg = dataclasses.replace(_config(), triage=None)

    decision = await run_triaged_from_intake(
        _stub_intake(),
        config=cfg,
        program="cardiovita-pap-2026",
        tenant_id="test-tenant",
        rules=[],
    )

    assert triage_called[0] is False
    assert decision.triage is None
    assert decision.outcome == "approve"
