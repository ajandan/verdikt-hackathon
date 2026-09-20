"""Unit tests for the design-time rule translator.

Locks the contract for :mod:`pap_pipeline.rule_translator`:

* A valid LLM response (formula + notes) round-trips through pydantic
  validation and returns as a ``TranslationResult`` the save handler
  can persist.
* HTTP failures, malformed JSON, and formula-validation failures each
  return ``formula=None`` with an actionable note.
* Empty notes + valid formula = green light for the force-author gate
  (Chunk 4).
"""
from __future__ import annotations

import json

import httpx
import pytest

from pap_pipeline import rule_translator as rt


def _config() -> rt.TranslatorConfig:
    return rt.TranslatorConfig(
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model="test-model",
    )


def _patch_client(monkeypatch, handler) -> list[dict]:
    """Route AsyncClient calls through httpx.MockTransport so tests
    can assert on payload shape without touching the network."""
    calls: list[dict] = []
    real = httpx.AsyncClient

    def _wrapped(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        return handler(request)

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_wrapped)
        return real(*args, **kwargs)

    monkeypatch.setattr(rt.httpx, "AsyncClient", _factory)
    return calls


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_translate_returns_formula_when_llm_reply_is_valid(monkeypatch) -> None:
    def _handler(_req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "formula": {
                "op": "AND",
                "terms": [
                    {"op": "==", "field": "ins_uninsured", "value": True},
                    {"op": "in", "field": "patient_state", "values": ["CA", "NY", "TX"]},
                ],
            },
            "notes": [],
        })}}]})

    _patch_client(monkeypatch, _handler)
    r = await rt.translate_criterion_to_formula(
        criterion="uninsured applicant in a US state",
        exception="",
        fields=["ins_uninsured", "patient_state"],
        schema_slots=["ins_uninsured", "patient_state", "rx_medication"],
        config=_config(),
    )
    assert r.formula is not None
    assert r.formula["op"] == "AND"
    assert r.notes == []


@pytest.mark.asyncio
async def test_translate_carries_notes_for_dropped_clauses(monkeypatch) -> None:
    """When the LLM covers most of a criterion but drops a fuzzy clause,
    it returns the formula + a note naming the dropped text. Author
    revises the criterion (or acknowledges the drop) before saving."""
    def _handler(_req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "formula": {"op": "==", "field": "ins_uninsured", "value": True},
            "notes": ["Dropped clause: 'at reviewer's discretion' (no comparable op)"],
        })}}]})

    _patch_client(monkeypatch, _handler)
    r = await rt.translate_criterion_to_formula(
        criterion="uninsured, and at reviewer's discretion approved",
        fields=["ins_uninsured"],
        schema_slots=["ins_uninsured"],
        config=_config(),
    )
    assert r.formula is not None
    assert len(r.notes) == 1
    assert "at reviewer's discretion" in r.notes[0]


# ---------------------------------------------------------------------------
# Failure modes — each returns formula=None with an actionable note.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_translate_returns_none_when_llm_signals_no_coverage(monkeypatch) -> None:
    def _handler(_req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "formula": None,
            "notes": ["Entire criterion depends on external policy — no clause is expressible."],
        })}}]})

    _patch_client(monkeypatch, _handler)
    r = await rt.translate_criterion_to_formula(
        criterion="reviewer decides based on policy",
        config=_config(),
    )
    assert r.formula is None
    assert r.notes and "policy" in r.notes[0]


@pytest.mark.asyncio
async def test_translate_returns_none_when_response_is_not_json(monkeypatch) -> None:
    def _handler(_req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json at all"}}]})

    _patch_client(monkeypatch, _handler)
    r = await rt.translate_criterion_to_formula(criterion="anything", config=_config())
    assert r.formula is None
    assert r.notes and "non-JSON" in r.notes[0]


@pytest.mark.asyncio
async def test_translate_returns_none_when_formula_fails_pydantic_validation(monkeypatch) -> None:
    def _handler(_req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            # invalid: `==` requires `value`, not `values`.
            "formula": {"op": "==", "field": "x", "values": [1, 2]},
            "notes": [],
        })}}]})

    _patch_client(monkeypatch, _handler)
    r = await rt.translate_criterion_to_formula(criterion="foo", config=_config())
    assert r.formula is None
    assert any("validation" in n.lower() for n in r.notes)


@pytest.mark.asyncio
async def test_translate_returns_none_on_http_failure(monkeypatch) -> None:
    def _handler(_req):
        raise httpx.ConnectError("connection refused")

    _patch_client(monkeypatch, _handler)
    r = await rt.translate_criterion_to_formula(criterion="foo", config=_config())
    assert r.formula is None
    assert r.notes and "unreachable" in r.notes[0].lower()


# ---------------------------------------------------------------------------
# Wire contract — prompt + payload shape asserted so a future change
# doesn't accidentally strip the grammar spec from the system prompt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wire_body_carries_grammar_prompt_and_user_payload(monkeypatch) -> None:
    def _handler(_req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "formula": {"op": "==", "field": "x", "value": 1},
            "notes": [],
        })}}]})

    calls = _patch_client(monkeypatch, _handler)
    await rt.translate_criterion_to_formula(
        criterion="x equals 1",
        fields=["x"],
        schema_slots=["x", "y"],
        config=_config(),
    )
    body = calls[0]["body"]
    system_msg = body["messages"][0]["content"]
    user_msg = body["messages"][1]["content"]
    # System prompt must name the grammar so the LLM has the vocabulary.
    assert "Allowed ops" in system_msg
    assert "within_days" in system_msg
    assert "matches_regex" in system_msg
    # User payload must carry the criterion + the schema slot list so
    # the LLM can validate field names.
    user_data = json.loads(user_msg)
    assert user_data["criterion"] == "x equals 1"
    assert user_data["rule_declared_fields"] == ["x"]
    assert user_data["program_schema_slots"] == ["x", "y"]
    # JSON-mode + non-reasoning must both be set.
    assert body["response_format"] == {"type": "json_object"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
