"""Orchestrator — retrieve + reason composition.

The orchestrator is where the three stages meet. Even without Stage 1
(extract) wired, the retrieve→reason slice must run end-to-end from a
prepared Intake and the reranker's scores must land on Decision citations.
These tests lock that composition so a stage swap doesn't silently break
the score-attachment step, which the reply letter's citation ordering
depends on.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pap_pipeline.demo_data import load_canonical_intake
from pap_pipeline.orchestrator import (
    PipelineConfig,
    _extras_from_extraction,
    _intake_from_extraction,
    run,
    run_from_intake,
    run_with_intake,
)
from pap_pipeline.stages import extract as extract_mod
from pap_pipeline.stages import reason as reason_mod
from pap_pipeline.stages import retrieve as retrieve_mod
from pap_pipeline.stages.extract import ExtractClientConfig
from pap_pipeline.stages.reason import ReasonClientConfig
from pap_pipeline.stages.retrieve import RetrieveClientConfig


def _config() -> PipelineConfig:
    return PipelineConfig(
        extract=ExtractClientConfig(
            base_url="https://api.studio.nebius.com/v1",
            api_key="nb-secret",
        ),
        retrieve=RetrieveClientConfig(
            base_url="https://api.studio.nebius.com/v1",
            api_key="nb-secret",
        ),
        reason=ReasonClientConfig(
            base_url="https://api.studio.nebius.com/v1",
            api_key="nb-secret",
            model="nvidia/nemotron-3-super-120b-a12b",
        ),
    )


def _patch_all_stages(monkeypatch, handler):
    """Patch all three stages' httpx to route through a shared MockTransport.

    Capture the real class before patching — otherwise `_factory` would
    recurse into itself once the module attribute is replaced.
    """
    real_async = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async(*args, **kwargs)

    monkeypatch.setattr(extract_mod.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(retrieve_mod.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(reason_mod.httpx, "AsyncClient", _factory)


# Backward-compat alias for the middle-slice tests.
_patch_both = _patch_all_stages


@pytest.mark.asyncio
async def test_orchestrator_runs_full_slice_and_stitches_scores(monkeypatch) -> None:
    """Given retrieve returns four rules with descending scores and reason
    approves all four, the returned Decision must carry those scores on the
    citations, in the reason stage's order — not the retrieval order."""
    rule_scores = {
        "CV-INC-01": 0.97,
        "CV-INS-01": 0.91,
        "CV-RES-01": 0.83,
        "CV-CLIN-01": 0.72,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = json.loads(request.content)
        if p.endswith("/embeddings"):
            # Unit vectors — corpus of size 4 + query = 5 inputs expected.
            n = len(body["input"])
            return httpx.Response(200, json={"data": [
                {"embedding": [1.0 if i == j else 0.0 for j in range(n)]}
                for i in range(n)
            ]})
        if p.endswith("/rerank"):
            # Reranker sees documents in embed order (the corpus order the
            # retrieve stage passed in). Rule ids show up as `[{id}]` at the
            # head of each prompt block — scan for our target ids rather
            # than hard-coding a filename order that breaks when new rules
            # land in the pack.
            docs = body["documents"]
            order = ["CV-INC-01", "CV-INS-01", "CV-RES-01", "CV-CLIN-01"]
            results = []
            for target in order:
                idx = next(
                    (i for i, d in enumerate(docs) if d.startswith(f"[{target}]")),
                    None,
                )
                if idx is None:
                    continue
                results.append({"index": idx, "relevance_score": rule_scores[target]})
            return httpx.Response(200, json={"results": results})
        if p.endswith("/chat/completions"):
            content = json.dumps({
                "outcome": "approve",
                "citations": [
                    {"rule_id": "CV-INC-01", "matched": True, "score": None},
                    {"rule_id": "CV-INS-01", "matched": True, "score": None},
                    {"rule_id": "CV-RES-01", "matched": True, "score": None},
                    {"rule_id": "CV-CLIN-01", "matched": True, "score": None},
                ],
                "missing_fields": [],
                "rationale": "All four Cardiovita rules pass.",
            })
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(404)

    _patch_both(monkeypatch, _handler)

    decision = await run_from_intake(load_canonical_intake(), config=_config())

    assert decision.outcome == "approve"
    assert [c.rule_id for c in decision.citations] == [
        "CV-INC-01", "CV-INS-01", "CV-RES-01", "CV-CLIN-01",
    ]
    # Reranker scores stitched onto citations by rule_id.
    for c in decision.citations:
        assert c.score == rule_scores[c.rule_id], c


@pytest.mark.asyncio
async def test_orchestrator_leaves_score_none_when_reason_cites_unretrieved_rule(monkeypatch) -> None:
    """If the reason stage names a rule the retriever didn't surface, the
    orchestrator must leave its score as None rather than fabricate one."""
    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = json.loads(request.content)
        if p.endswith("/embeddings"):
            n = len(body["input"])
            return httpx.Response(200, json={"data": [
                {"embedding": [1.0 if i == j else 0.0 for j in range(max(n, 2))][:max(n, 2)]}
                for i in range(n)
            ]})
        if p.endswith("/rerank"):
            return httpx.Response(200, json={
                "results": [{"index": 0, "relevance_score": 0.9}],
            })
        if p.endswith("/chat/completions"):
            content = json.dumps({
                "outcome": "deny",
                "citations": [
                    {"rule_id": "PHANTOM-RULE", "matched": False, "score": None},
                ],
                "missing_fields": [],
                "rationale": "Hallucinated citation.",
            })
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(404)

    _patch_both(monkeypatch, _handler)

    decision = await run_from_intake(
        load_canonical_intake(),
        config=_config(),
        program="cardiovita-pap-2026",
        k=1,
    )
    assert len(decision.citations) == 1
    assert decision.citations[0].score is None


@pytest.mark.asyncio
async def test_orchestrator_run_end_to_end_from_attachments(monkeypatch) -> None:
    """Full pipeline from raw image bytes → Decision: one extract call
    (vision) + one embed + one rerank + one reason. Locks the wire count so
    a stage that fires twice would fail loudly."""
    call_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = json.loads(request.content)
        # Extract is a /chat/completions call whose body carries an image_url
        # part; reason is /chat/completions with a text-only user message.
        if p.endswith("/chat/completions"):
            first_msg = body["messages"][0]
            is_vision = isinstance(first_msg.get("content"), list) and any(
                part.get("type") == "image_url" for part in first_msg["content"]
            )
            if is_vision:
                call_paths.append("extract")
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                    "patient_name": "Maria Chen",
                    "household_size": 2,
                    "annual_household_income": 60000.00,
                    "insurance_status": "uninsured",
                    "has_denial_letter": False,
                    "country_of_residence": "United States",
                    "mailing_state": "California",
                    "prescribed_drug": "Cardiovita",
                    "prescriber_npi": "1234567893",
                    "prescription_date": "2026-08-15",
                })}}]})
            call_paths.append("reason")
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "outcome": "approve",
                "citations": [
                    {"rule_id": "CV-INC-01", "matched": True, "score": None},
                    {"rule_id": "CV-INS-01", "matched": True, "score": None},
                    {"rule_id": "CV-RES-01", "matched": True, "score": None},
                    {"rule_id": "CV-CLIN-01", "matched": True, "score": None},
                ],
                "missing_fields": [],
                "rationale": "All four Cardiovita rules pass.",
            })}}]})
        if p.endswith("/embeddings"):
            call_paths.append("embed")
            n = len(body["input"])
            return httpx.Response(200, json={"data": [
                {"embedding": [1.0 if i == j else 0.0 for j in range(n)]}
                for i in range(n)
            ]})
        if p.endswith("/rerank"):
            call_paths.append("rerank")
            return httpx.Response(200, json={"results": [
                {"index": i, "relevance_score": 1.0 - i * 0.1}
                for i in range(len(body["documents"]))
            ]})
        return httpx.Response(404)

    _patch_all_stages(monkeypatch, _handler)

    # A single tiny PNG stands in for the applicant's attachment.
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x9c\x8a\x00\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    decision = await run([png], config=_config(), program="cardiovita-pap-2026")

    assert decision.outcome == "approve"
    assert [c.rule_id for c in decision.citations] == [
        "CV-INC-01", "CV-INS-01", "CV-RES-01", "CV-CLIN-01",
    ]
    # extract → embed → rerank → reason, exactly once each.
    assert call_paths == ["extract", "embed", "rerank", "reason"]


def test_intake_boundary_drops_invalid_field_but_keeps_the_rest() -> None:
    """A bogus enum from the VLM shouldn't wipe the whole intake — the
    boundary validator retries field-by-field and drops just the offender.
    This behavior used to live inside the extract stage; it moved here
    when extract's contract changed from Intake to raw dict."""
    intake = _intake_from_extraction({
        "patient_name": "Maria Chen",
        "insurance_status": "priv",  # invalid enum
        "household_size": 2,
    })
    assert intake.patient_name == "Maria Chen"
    assert intake.household_size == 2
    assert intake.insurance_status is None


def test_intake_boundary_ignores_unknown_slot_names() -> None:
    """Program-scoped slot names that don't map onto Intake are dropped from
    the coerced Intake — they flow through the parallel extras path instead
    so the reason stage still sees them without polluting Intake."""
    intake = _intake_from_extraction({
        "patient_name": "Maria Chen",
        "member_id": "MC-42",  # program-specific slot, not on Intake
    })
    assert intake.patient_name == "Maria Chen"
    assert not hasattr(intake, "member_id")


def test_extras_from_extraction_keeps_only_non_intake_keys() -> None:
    """Extras is the complement of Intake — canonical fields go through the
    Intake coercion; program-defined fields end up here so the reason stage
    and reply mapper can still read them."""
    extras = _extras_from_extraction({
        "patient_name": "Maria Chen",       # canonical → dropped
        "member_id": "MC-42",               # program-specific → kept
        "insurance_status": "commercial",   # canonical → dropped
        "referring_provider": "Dr. Kim",    # program-specific → kept
    })
    assert extras == {"member_id": "MC-42", "referring_provider": "Dr. Kim"}


def test_extras_from_extraction_handles_empty_input() -> None:
    """None or {} in → {} out. Prevents a `NoneType has no items` on a
    tenant whose program schema hasn't been populated yet."""
    assert _extras_from_extraction({}) == {}
    assert _extras_from_extraction(None) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_with_intake_returns_raw_dict_intake_and_decision(monkeypatch) -> None:
    """`run_with_intake` returns the tuple the inbox hook depends on:
    (raw_extraction_dict, coerced_Intake, Decision). The raw dict must
    include program-specific slots that Intake doesn't carry so the reply
    mapper and detail UI can render them."""
    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = json.loads(request.content)
        if p.endswith("/chat/completions"):
            first_msg = body["messages"][0]
            is_vision = isinstance(first_msg.get("content"), list) and any(
                part.get("type") == "image_url" for part in first_msg["content"]
            )
            if is_vision:
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                    "patient_name": "Maria Chen",
                    "member_id": "MC-42",  # program-specific slot
                })}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "outcome": "approve",
                "citations": [],
                "missing_fields": [],
                "rationale": "ok",
            })}}]})
        if p.endswith("/embeddings"):
            n = len(body["input"])
            return httpx.Response(200, json={"data": [
                {"embedding": [1.0 if i == j else 0.0 for j in range(n)]}
                for i in range(n)
            ]})
        if p.endswith("/rerank"):
            return httpx.Response(200, json={"results": [
                {"index": i, "relevance_score": 1.0 - i * 0.1}
                for i in range(len(body["documents"]))
            ]})
        return httpx.Response(404)

    _patch_all_stages(monkeypatch, _handler)

    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x9c\x8a\x00\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    # Explicit slots list — extract stage filters returned keys to those it
    # was asked to look for, so `member_id` needs to appear here to survive.
    slots = [
        {"name": "patient_name", "type": "string", "description": "Full name."},
        {"name": "member_id", "type": "string", "description": "Program member ID."},
    ]
    raw, intake, decision = await run_with_intake(
        [png], config=_config(), program="cardiovita-pap-2026", slots=slots
    )

    # Raw dict preserves the program-specific slot the VLM lifted.
    assert raw["member_id"] == "MC-42"
    assert raw["patient_name"] == "Maria Chen"
    # Intake only carries canonical fields.
    assert intake.patient_name == "Maria Chen"
    assert not hasattr(intake, "member_id")
    # Decision is what the reason stub returned.
    assert decision.outcome == "approve"
