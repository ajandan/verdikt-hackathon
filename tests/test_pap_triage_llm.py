"""LLM-mode triage classifier — mocked Nebius Token Factory chat endpoint.

Covers the LLM path added on top of the rule-based safety net:

- successful classification with high confidence → LLM answer wins
- low confidence → fall back to rule-based classifier
- HTTP failure → fall back to rule-based classifier
- invalid JSON in the LLM response → fall back
- unknown case_type from the LLM → fall back
"""
from __future__ import annotations

import json

import httpx
import pytest

from pap_pipeline.schemas.intake import Intake
from pap_pipeline.stages import triage as triage_mod
from pap_pipeline.stages.triage import TriageConfig, triage

_LLM_CFG = TriageConfig(
    base_url="https://nebius.example.com/v1",
    api_key="test-key",
    model="nvidia/nemotron-nano-3-6b",
    min_confidence=0.6,
    timeout=5.0,
)


def _rich_intake() -> Intake:
    return Intake(
        patient_name="Devon Okafor",
        family_name="Okafor",
        prescribed_drug="Cardiovita",
        prescriber_npi="1234567890",
    )


def _install_mock_response(
    monkeypatch: pytest.MonkeyPatch, response_body: dict | str, status: int = 200
):
    """Replace httpx.AsyncClient with one whose POSTs return the given body.

    ``response_body`` as dict → serialized to JSON; as str → returned raw
    (used to test bad-JSON fallback).
    """
    real_async = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response_body, dict):
            return httpx.Response(status, json=response_body)
        return httpx.Response(status, content=response_body.encode("utf-8"))

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async(*args, **kwargs)

    monkeypatch.setattr(triage_mod.httpx, "AsyncClient", _factory)


def _install_failing_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """AsyncClient raises on send — exercises the HTTPError → rules fallback."""
    real_async = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async(*args, **kwargs)

    monkeypatch.setattr(triage_mod.httpx, "AsyncClient", _factory)


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_high_confidence_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A confident LLM classification is used directly, rules don't run."""
    _install_mock_response(monkeypatch, {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "case_type": "appeal",
                    "confidence": 0.92,
                    "reason": "subject explicitly says 'appeal of denial'",
                }),
            },
        }],
    })
    result = await triage(_rich_intake(), config=_LLM_CFG, subject="Refill please")
    # LLM said 'appeal' — subject regex would have said 'refill'. LLM wins.
    assert result.case_type == "appeal"
    assert result.route == "crm_appeals"
    assert result.signals["classifier"] == "llm"
    assert result.signals["confidence"] == 0.92


@pytest.mark.asyncio
async def test_llm_low_confidence_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM returns valid case_type but under the confidence threshold → rules run."""
    _install_mock_response(monkeypatch, {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "case_type": "appeal",
                    "confidence": 0.30,   # below default 0.6
                    "reason": "unsure",
                }),
            },
        }],
    })
    result = await triage(_rich_intake(), config=_LLM_CFG, subject="Refill please")
    # Rules take over: subject "refill" wins
    assert result.case_type == "refill"
    assert result.signals["classifier"] == "rules"


@pytest.mark.asyncio
async def test_llm_http_failure_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_failing_transport(monkeypatch)
    result = await triage(_rich_intake(), config=_LLM_CFG, subject="Refill please")
    assert result.case_type == "refill"
    assert result.signals["classifier"] == "rules"


@pytest.mark.asyncio
async def test_llm_bad_json_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM returns malformed content (e.g. LLM wrapped JSON in prose) → rules."""
    _install_mock_response(monkeypatch, {
        "choices": [{"message": {"content": "Sure! Here's the answer: appeal."}}]
    })
    result = await triage(_rich_intake(), config=_LLM_CFG, subject="Refill please")
    assert result.signals["classifier"] == "rules"


@pytest.mark.asyncio
async def test_llm_unknown_case_type_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM invents a case_type outside the fixed vocabulary → rules take over."""
    _install_mock_response(monkeypatch, {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "case_type": "hallucinated",   # not in the vocab
                    "confidence": 0.99,
                    "reason": "made up",
                }),
            },
        }],
    })
    result = await triage(_rich_intake(), config=_LLM_CFG, subject="Refill please")
    assert result.signals["classifier"] == "rules"


@pytest.mark.asyncio
async def test_llm_5xx_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server-side errors (5xx) trigger raise_for_status → rules."""
    _install_mock_response(
        monkeypatch,
        {"error": "internal"},
        status=500,
    )
    result = await triage(_rich_intake(), config=_LLM_CFG, subject="Refill please")
    assert result.signals["classifier"] == "rules"


@pytest.mark.asyncio
async def test_no_llm_creds_skips_llm_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When any of base_url/api_key/model is missing, LLM path is skipped
    entirely — the mock transport should never fire."""
    fired = []

    def _handler(request: httpx.Request) -> httpx.Response:
        fired.append(True)
        return httpx.Response(200, json={"choices": []})

    real_async = httpx.AsyncClient
    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async(*args, **kwargs)
    monkeypatch.setattr(triage_mod.httpx, "AsyncClient", _factory)

    # No api_key → LLM skipped
    cfg = TriageConfig(base_url="x", model="y")
    result = await triage(_rich_intake(), config=cfg, subject="Refill please")
    assert result.case_type == "refill"
    assert result.signals["classifier"] == "rules"
    assert fired == []
