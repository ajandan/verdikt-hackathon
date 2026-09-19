"""Phase D — LLM-assist suggester for unresolved copy-to-program rows.

Pins the fail-open contract: every error path (missing creds, HTTP 5xx,
malformed JSON, hallucinated targets) yields an empty dict or a
``target=None`` suggestion so the wizard is never blocked by a flaky
model call. Also pins the happy-path shape the wizard route relies on.

The suggester itself doesn't decide anything — the operator still
reviews. These tests guard the "hint quality + fail-open" contract, not
the operator UX (that's covered in ``tests/test_rule_copy_wizard.py``).
"""
from __future__ import annotations

import json

import httpx
import pytest

from pap_pipeline import rule_mapping_llm
from pap_pipeline.rule_mapping_llm import LLMSuggestion, suggest_unresolved

_TARGET_SLOTS = [
    {"name": "insurance_status", "type": "enum",
     "description": "Applicant's insurance category (uninsured / medicare / commercial)."},
    {"name": "prescribed_drug", "type": "string",
     "description": "The drug the prescription is written for."},
    {"name": "patient_zip", "type": "string",
     "description": "Applicant's mailing ZIP code."},
]


def _install_mock_response(
    monkeypatch: pytest.MonkeyPatch, response_body: dict | str, status: int = 200,
) -> None:
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

    monkeypatch.setattr(rule_mapping_llm.httpx, "AsyncClient", _factory)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_happy_path_returns_valid_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model returns well-formed JSON — every suggestion has a real
    target slot + reasoning, and hallucinated names never appear."""
    _install_mock_response(monkeypatch, {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "mappings": [
                        {"source": "ins_uninsured", "target": "insurance_status",
                         "confidence": 0.92,
                         "reasoning": "Both name the applicant's insurance category."},
                        {"source": "rx_medication", "target": "prescribed_drug",
                         "confidence": 0.85,
                         "reasoning": "Both name the prescribed drug."},
                    ],
                }),
            },
        }],
    })
    out = await suggest_unresolved(
        ["ins_uninsured", "rx_medication"],
        _TARGET_SLOTS,
        rule_criterion="@ins_uninsured is true AND @rx_medication is Cardiovita",
        rule_exception="",
        base_url="https://nebius.example.com/v1",
        api_key="test-key",
        model="nvidia/nemotron-nano-3-6b",
    )
    assert set(out.keys()) == {"ins_uninsured", "rx_medication"}
    ins = out["ins_uninsured"]
    assert isinstance(ins, LLMSuggestion)
    assert ins.target == "insurance_status"
    assert ins.confidence == pytest.approx(0.92)
    assert "insurance" in ins.reasoning.lower()
    rx = out["rx_medication"]
    assert rx.target == "prescribed_drug"
    assert rx.confidence == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# Guardrails — the LLM lies, we don't propagate the lie
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_hallucinated_target_is_dropped_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM invents a slot name that isn't on the target schema →
    suggestion is kept but its target is coerced to None so the wizard
    can render a 'no confident guess' state instead of falsely
    pre-selecting a fake option."""
    _install_mock_response(monkeypatch, {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "mappings": [
                        {"source": "ins_uninsured",
                         "target": "totally_made_up_slot",   # not in schema
                         "confidence": 0.99,
                         "reasoning": "I know things."},
                    ],
                }),
            },
        }],
    })
    out = await suggest_unresolved(
        ["ins_uninsured"],
        _TARGET_SLOTS,
        rule_criterion="@ins_uninsured is true",
        rule_exception="",
        base_url="https://nebius.example.com/v1",
        api_key="test-key",
        model="nvidia/nemotron-nano-3-6b",
    )
    assert "ins_uninsured" in out
    assert out["ins_uninsured"].target is None


@pytest.mark.asyncio
async def test_llm_confidence_is_clipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-range confidence values are clipped to [0, 1] before the
    suggestion is returned — the wizard trusts this range."""
    _install_mock_response(monkeypatch, {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "mappings": [
                        {"source": "ins_uninsured", "target": "insurance_status",
                         "confidence": 1.7, "reasoning": "hyper-confident."},
                    ],
                }),
            },
        }],
    })
    out = await suggest_unresolved(
        ["ins_uninsured"],
        _TARGET_SLOTS,
        rule_criterion="@ins_uninsured is true",
        rule_exception="",
        base_url="https://nebius.example.com/v1",
        api_key="test-key",
        model="nvidia/nemotron-nano-3-6b",
    )
    assert out["ins_uninsured"].confidence == 1.0


# ---------------------------------------------------------------------------
# Fail-open — every error path returns {}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_http_5xx_returns_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model backend is down → empty dict, no exception raised, wizard
    falls back to unresolved-row rendering."""
    _install_mock_response(monkeypatch, {"error": "service unavailable"}, status=503)
    out = await suggest_unresolved(
        ["ins_uninsured"],
        _TARGET_SLOTS,
        rule_criterion="@ins_uninsured",
        rule_exception="",
        base_url="https://nebius.example.com/v1",
        api_key="test-key",
        model="nvidia/nemotron-nano-3-6b",
    )
    assert out == {}


@pytest.mark.asyncio
async def test_llm_malformed_json_returns_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM wrapped JSON in prose / returned garbage → empty dict."""
    _install_mock_response(monkeypatch, {
        "choices": [{
            "message": {"content": "Sure, here's my answer! It's ins_uninsured."},
        }],
    })
    out = await suggest_unresolved(
        ["ins_uninsured"],
        _TARGET_SLOTS,
        rule_criterion="@ins_uninsured",
        rule_exception="",
        base_url="https://nebius.example.com/v1",
        api_key="test-key",
        model="nvidia/nemotron-nano-3-6b",
    )
    assert out == {}


@pytest.mark.asyncio
async def test_llm_missing_creds_skips_http_call() -> None:
    """No base_url / api_key / model → return {} without touching the
    network. Zero-behavior-change guarantee for deployments without
    Nebius creds configured."""
    # No monkeypatch — if the function tried to hit the network,
    # httpx would fail to resolve nebius.example.com in the test env
    # and the test would either hang or error. We assert it returns
    # instantly with no HTTP call.
    out = await suggest_unresolved(
        ["ins_uninsured", "rx_medication"],
        _TARGET_SLOTS,
        rule_criterion="@ins_uninsured",
        rule_exception="",
        base_url="",
        api_key="",
        model="",
    )
    assert out == {}


@pytest.mark.asyncio
async def test_llm_empty_source_refs_returns_empty_dict() -> None:
    """No unresolved refs → no work to do, no HTTP call. Belt-and-
    suspenders for the wizard route's own guard."""
    out = await suggest_unresolved(
        [],
        _TARGET_SLOTS,
        rule_criterion="@ins_uninsured",
        rule_exception="",
        base_url="https://nebius.example.com/v1",
        api_key="test-key",
        model="nvidia/nemotron-nano-3-6b",
    )
    assert out == {}


@pytest.mark.asyncio
async def test_llm_hallucinated_source_ref_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM returns a mapping for a source ref we didn't ask about →
    silently ignored so the wizard doesn't get surprise rows."""
    _install_mock_response(monkeypatch, {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "mappings": [
                        {"source": "not_a_real_source",
                         "target": "insurance_status",
                         "confidence": 0.9, "reasoning": "guess"},
                    ],
                }),
            },
        }],
    })
    out = await suggest_unresolved(
        ["ins_uninsured"],
        _TARGET_SLOTS,
        rule_criterion="@ins_uninsured",
        rule_exception="",
        base_url="https://nebius.example.com/v1",
        api_key="test-key",
        model="nvidia/nemotron-nano-3-6b",
    )
    assert out == {}  # the ONE mapping was for a bogus source
