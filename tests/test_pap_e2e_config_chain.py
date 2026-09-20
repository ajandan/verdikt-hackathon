"""End-to-end regression: the 3-level model config chain reaches the wire.

These tests are the counterweight to the unit-tests around
:func:`resolve_pipeline_models`. Unit tests prove the resolver PICKS the
right model at each level; these tests prove the pipeline actually SENDS
that model to Nebius when a real case walks the stages.

Strategy: seed a Tenant + Program with mixed override state, patch every
stage's ``httpx.AsyncClient`` with a single MockTransport that captures
the ``body["model"]`` on each call, then drive the pipeline end-to-end
via :func:`pap_pipeline.pipeline_config.build_pipeline_config` +
:func:`run_with_intake`. The transport records
``(path, model)`` per request so the assertions can pin exact wire values
per stage.

Any regression in inbox_hook's config-build path (dropped ``resolved.*``
threading, hard-coded platform default, silent NULL) surfaces here as a
model mismatch on the captured call log.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import delete

from docgen.config import Settings
from docgen.store.db import sync_session
from docgen.store.models import Program, Tenant
from pap_pipeline.pipeline_config import build_pipeline_config
from pap_pipeline.orchestrator import run_with_intake
from pap_pipeline.stages import extract as extract_mod
from pap_pipeline.stages import reason as reason_mod
from pap_pipeline.stages import retrieve as retrieve_mod
from pap_pipeline.stages import triage as triage_mod

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _settings() -> Settings:
    """Platform-default Settings — represents the env-var baseline the
    resolver falls back to when neither tenant nor program overrides
    are set. Every value is a fake sentinel so a real Nebius URL can't
    accidentally be hit if MockTransport misses a path."""
    return Settings(
        ai_base_url="http://test-nebius.local/v1",
        ai_api_key="test-key",
        pdf_vision_ocr_model="SETTINGS-EXTRACT",
        ai_verdict_model="SETTINGS-REASON",
        triage_model="SETTINGS-TRIAGE",
    )


def _seed(
    *,
    tenant_extract: str | None = None,
    tenant_triage: str | None = None,
    tenant_reason: str | None = None,
    tenant_verifier: str | None = None,
    tenant_triage_routing: dict | None = None,
    program_extract: str | None = None,
    program_triage: str | None = None,
    program_reason: str | None = None,
    program_verifier: str | None = None,
    program_triage_routing: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    tid = uuid.uuid4()
    pid = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(
            id=tid, name=f"e2e-t-{tid.hex[:6]}",
            extract_model=tenant_extract,
            triage_model=tenant_triage,
            reason_model=tenant_reason,
            verifier_model=tenant_verifier,
            triage_routing=tenant_triage_routing,
        ))
        s.flush()
        s.add(Program(
            id=pid, tenant_id=tid,
            slug=f"p-{tid.hex[:6]}",
            name=f"prog-{tid.hex[:6]}",
            extract_model=program_extract,
            triage_model=program_triage,
            reason_model=program_reason,
            verifier_extract_model=program_verifier,
            triage_routing=program_triage_routing,
            extraction_schema=[],
            history_slots=[],
        ))
        s.commit()
    return tid, pid


def _teardown(tid: uuid.UUID) -> None:
    with sync_session() as s:
        s.execute(delete(Program).where(Program.tenant_id == tid))
        s.execute(delete(Tenant).where(Tenant.id == tid))
        s.commit()


def _load(pid: uuid.UUID) -> tuple[Tenant, Program]:
    with sync_session() as s:
        prog = s.get(Program, pid)
        tenant = s.get(Tenant, prog.tenant_id)
        # Detach from session so caller can use these across event loops.
        s.expunge(prog)
        s.expunge(tenant)
        return tenant, prog


# ---------------------------------------------------------------------------
# Wire-capture MockTransport
# ---------------------------------------------------------------------------

class _StageWireCapture:
    """MockTransport handler that records ``(stage, model_from_body)`` on
    every intercepted call and returns a canned response per stage.

    Stage identification:
      * ``/embeddings`` → retrieve.embed
      * ``/rerank``     → retrieve.rerank
      * ``/chat/completions`` with vision message → extract (or verifier)
      * ``/chat/completions`` with a ``response_format`` json_schema and
        the Triage-classifier prompt in the system message → triage
      * ``/chat/completions`` otherwise → reason
    """

    def __init__(self) -> None:
        # List of dicts to preserve call order + all details for assertions.
        self.calls: list[dict[str, Any]] = []

    def _classify(self, body: dict) -> str:
        first_msg = body.get("messages", [{}])[0]
        content = first_msg.get("content", "")
        # Triage stage sets a distinctive system prompt naming case types.
        system_content = ""
        if isinstance(body.get("messages"), list):
            for m in body["messages"]:
                if m.get("role") == "system":
                    system_content += str(m.get("content", ""))
        # Triage system prompt has a distinctive header naming the classifier.
        if "case-triage classifier" in system_content.lower():
            return "triage"
        # Vision extract sends a multipart content list with an image_url part.
        if isinstance(content, list) and any(
            p.get("type") == "image_url" for p in content
        ):
            return "extract"
        return "reason"

    def _response_for(self, stage: str, body: dict) -> httpx.Response:
        if stage == "triage":
            # Return the canonical shape triage._llm_classify expects.
            payload = {"case_type": "new_intake", "confidence": 0.95}
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(payload)}}],
            })
        if stage == "extract":
            payload = {
                "patient_name": "Devon Okafor",
                "family_name": "Okafor",
                "prescribed_drug": "Cardiovita",
                "insurance_status": "uninsured",
                "household_size": 2,
                "annual_household_income": 40000.00,
                "country_of_residence": "United States",
                "mailing_state": "California",
            }
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(payload)}}],
            })
        if stage == "reason":
            payload = {
                "outcome": "approve",
                "citations": [],
                "missing_fields": [],
                "rationale": "e2e stub verdict",
            }
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(payload)}}],
            })
        return httpx.Response(404)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        try:
            body = json.loads(request.content) if request.content else {}
        except Exception:
            body = {}

        if path.endswith("/embeddings"):
            n = len(body.get("input", []))
            self.calls.append({
                "stage": "retrieve.embed", "path": path,
                "model": body.get("model"),
            })
            return httpx.Response(200, json={"data": [
                {"embedding": [1.0 if i == j else 0.0 for j in range(max(n, 2))][:max(n, 2)]}
                for i in range(n)
            ]})
        if path.endswith("/rerank"):
            self.calls.append({
                "stage": "retrieve.rerank", "path": path,
                "model": body.get("model"),
            })
            return httpx.Response(200, json={"results": []})
        if path.endswith("/chat/completions"):
            stage = self._classify(body)
            self.calls.append({
                "stage": stage, "path": path,
                "model": body.get("model"),
            })
            return self._response_for(stage, body)
        return httpx.Response(404)

    def models_for(self, stage: str) -> list[str | None]:
        return [c["model"] for c in self.calls if c["stage"] == stage]


def _patch_all(monkeypatch: pytest.MonkeyPatch, cap: _StageWireCapture) -> None:
    real = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(cap)
        return real(*args, **kwargs)

    monkeypatch.setattr(extract_mod.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(retrieve_mod.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(reason_mod.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(triage_mod.httpx, "AsyncClient", _factory)


# A tiny valid PNG so the extract stage has something to attach as image_url.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff"
    b"\x9c\x8a\x00\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# Tests — every one runs a full pipeline pass and pins per-stage wire models.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_program_overrides_win_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Program has extract + triage overrides; tenant has reason override;
    verifier is off. The wire capture proves each stage sees its own
    resolved model — not the tenant's, not the platform default."""
    tid, pid = _seed(
        program_extract="P-EXTRACT",
        program_triage="P-TRIAGE",
        tenant_reason="T-REASON",
    )
    try:
        tenant, program = _load(pid)
        cap = _StageWireCapture()
        _patch_all(monkeypatch, cap)
        cfg = build_pipeline_config(_settings(), tenant=tenant, program=program)

        _, _, decision = await run_with_intake(
            [_TINY_PNG], config=cfg,
            program=program.slug, tenant_id=str(tid),
            slots=None, rules=[],
        )

        assert decision.outcome == "approve"
        # Program-level extract override reached the vision call.
        assert cap.models_for("extract") == ["P-EXTRACT"]
        # Program-level triage override reached the classifier call.
        assert cap.models_for("triage") == ["P-TRIAGE"]
        # Tenant-level reason override reached the verdict call
        # (program didn't override → falls back to tenant, not settings).
        assert cap.models_for("reason") == ["T-REASON"]
    finally:
        _teardown(tid)


@pytest.mark.asyncio
async def test_all_null_falls_through_to_settings_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither tenant nor program set anything → every stage on the wire
    carries the platform-default (settings) model. Regression guard for
    an accidental hard-coded value in inbox_hook."""
    tid, pid = _seed()
    try:
        tenant, program = _load(pid)
        cap = _StageWireCapture()
        _patch_all(monkeypatch, cap)
        cfg = build_pipeline_config(_settings(), tenant=tenant, program=program)

        _, _, decision = await run_with_intake(
            [_TINY_PNG], config=cfg,
            program=program.slug, tenant_id=str(tid),
            slots=None, rules=[],
        )

        assert decision.outcome == "approve"
        assert cap.models_for("extract") == ["SETTINGS-EXTRACT"]
        assert cap.models_for("triage") == ["SETTINGS-TRIAGE"]
        assert cap.models_for("reason") == ["SETTINGS-REASON"]
    finally:
        _teardown(tid)


@pytest.mark.asyncio
async def test_tenant_override_beats_settings_when_program_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Program row exists but every model column is NULL. Tenant supplies
    all four → the pipeline uses the tenant values, not the platform
    defaults. Verifier is opt-in via tenant.verifier_model here."""
    tid, pid = _seed(
        tenant_extract="T-EXTRACT",
        tenant_triage="T-TRIAGE",
        tenant_reason="T-REASON",
        tenant_verifier="T-VERIFIER",
    )
    try:
        tenant, program = _load(pid)
        cap = _StageWireCapture()
        _patch_all(monkeypatch, cap)
        cfg = build_pipeline_config(_settings(), tenant=tenant, program=program)

        _, _, decision = await run_with_intake(
            [_TINY_PNG], config=cfg,
            program=program.slug, tenant_id=str(tid),
            slots=None, rules=[],
        )

        assert decision.outcome == "approve"
        # Extract fires TWICE when the verifier is on (primary + verifier
        # in parallel); both must carry a tenant-configured model.
        extract_models = cap.models_for("extract")
        assert set(extract_models) == {"T-EXTRACT", "T-VERIFIER"}, extract_models
        assert len(extract_models) == 2
        assert cap.models_for("triage") == ["T-TRIAGE"]
        assert cap.models_for("reason") == ["T-REASON"]
    finally:
        _teardown(tid)


@pytest.mark.asyncio
async def test_program_triage_routing_skip_decide_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Program overrides Triage routing so `new_intake` skips Decide and
    routes to ``reject``. End-to-end proof:

      * Reason stage is NEVER called (no /chat/completions for reason).
      * Decision.triage.route == "reject" and decision_needed is False.
      * decision.rationale carries the "Decide skipped" audit breadcrumb.
    """
    tid, pid = _seed(
        program_triage="P-TRIAGE",
        program_triage_routing={
            # Override the platform default: new_intake → reject, skip Decide.
            "new_intake": {"route": "reject", "decision_needed": False},
        },
    )
    try:
        tenant, program = _load(pid)
        cap = _StageWireCapture()
        _patch_all(monkeypatch, cap)
        cfg = build_pipeline_config(_settings(), tenant=tenant, program=program)

        _, _, decision = await run_with_intake(
            [_TINY_PNG], config=cfg,
            program=program.slug, tenant_id=str(tid),
            slots=None, rules=[],
        )

        # Triage fired with the program's override model.
        assert cap.models_for("triage") == ["P-TRIAGE"]
        # Extract still ran (Triage runs after Extract).
        assert cap.models_for("extract"), "extract stage should have fired"
        # Reason must NOT have been called — the skip-Decide path bypasses it.
        assert cap.models_for("reason") == [], cap.calls
        # Retrieve is part of Decide; also must not have fired.
        assert cap.models_for("retrieve.embed") == []
        # Decision carries the routing outcome for downstream systems.
        assert decision.triage is not None
        assert decision.triage.route == "reject"
        assert decision.triage.decision_needed is False
        assert "Decide skipped" in (decision.rationale or "")
    finally:
        _teardown(tid)


@pytest.mark.asyncio
async def test_tenant_verifier_wires_second_extract_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in verifier at the tenant level → orchestrator fires a second
    vision-extract call carrying the verifier model. Program has no
    verifier override so tenant.verifier_model is what reaches the wire."""
    tid, pid = _seed(
        tenant_extract="T-EXTRACT",
        tenant_verifier="T-VERIFIER-VLM",
    )
    try:
        tenant, program = _load(pid)
        cap = _StageWireCapture()
        _patch_all(monkeypatch, cap)
        cfg = build_pipeline_config(_settings(), tenant=tenant, program=program)

        _, _, _decision = await run_with_intake(
            [_TINY_PNG], config=cfg,
            program=program.slug, tenant_id=str(tid),
            slots=None, rules=[],
        )

        extract_models = cap.models_for("extract")
        assert sorted(extract_models) == sorted(["T-EXTRACT", "T-VERIFIER-VLM"])
    finally:
        _teardown(tid)


@pytest.mark.asyncio
async def test_program_extract_overrides_tenant_extract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Precedence check at the wire: program extract override wins over
    the tenant's own extract default. Regression for a resolver bug that
    would silently pick the tenant model when both layers set it."""
    tid, pid = _seed(
        tenant_extract="T-EXTRACT",
        program_extract="P-EXTRACT",
    )
    try:
        tenant, program = _load(pid)
        cap = _StageWireCapture()
        _patch_all(monkeypatch, cap)
        cfg = build_pipeline_config(_settings(), tenant=tenant, program=program)

        _, _, _decision = await run_with_intake(
            [_TINY_PNG], config=cfg,
            program=program.slug, tenant_id=str(tid),
            slots=None, rules=[],
        )

        assert cap.models_for("extract") == ["P-EXTRACT"]
        # Tenant's value never reached the wire.
        assert "T-EXTRACT" not in cap.models_for("extract")
    finally:
        _teardown(tid)
