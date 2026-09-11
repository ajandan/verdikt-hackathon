"""Stage 3 (reason) — Nemotron JSON verdict contract.

Locks in the wire shape and Decision parsing without touching the network.
The reasoning stage is the pipeline's decision authority: if the request body
drifts off the non-reasoning-mode flag, or Decision parsing quietly accepts
malformed JSON, the whole demo silently degrades. These tests exist so those
regressions surface as red builds, not as an empty rationale in the video.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import yaml

from pap_pipeline.demo_data import load_canonical_intake
from pap_pipeline.schemas import Decision, Rule
from pap_pipeline.stages import reason as reason_mod
from pap_pipeline.stages.reason import ReasonClientConfig, reason

RULES_DIR = (
    __import__("pathlib").Path(__file__).parent.parent
    / "src" / "pap_pipeline" / "rules"
)


def _load_rules() -> list[Rule]:
    return [
        Rule.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
        for p in sorted(RULES_DIR.glob("*.yaml"))
    ]


def _config() -> ReasonClientConfig:
    return ReasonClientConfig(
        base_url="https://api.studio.nebius.com/v1",
        api_key="nb-secret",
        model="nvidia/nemotron-3-super-120b-a12b",
    )


def _fake_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _patch_async_client(monkeypatch, handler) -> dict[str, Any]:
    """Replace httpx.AsyncClient with one bound to a MockTransport.

    Returns a mutable dict the handler writes into so tests can assert on
    what was sent.
    """
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return handler(request)

    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = _fake_transport(_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(reason_mod.httpx, "AsyncClient", _factory)
    return captured


@pytest.mark.asyncio
async def test_reason_posts_to_chat_completions_with_json_mode(monkeypatch) -> None:
    def _ok(_req):
        content = json.dumps({
            "outcome": "approve",
            "citations": [
                {"rule_id": "CV-INC-01", "matched": True, "score": None},
                {"rule_id": "CV-INS-01", "matched": True, "score": None},
                {"rule_id": "CV-RES-01", "matched": True, "score": None},
                {"rule_id": "CV-CLIN-01", "matched": True, "score": None},
            ],
            "missing_fields": [],
            "rationale": "All four Cardiovita rules pass for the applicant.",
        })
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    captured = _patch_async_client(monkeypatch, _ok)

    intake = load_canonical_intake()
    rules = _load_rules()
    decision = await reason(intake, rules, config=_config())

    assert isinstance(decision, Decision)
    assert decision.outcome == "approve"
    assert [c.rule_id for c in decision.citations] == [
        "CV-INC-01", "CV-INS-01", "CV-RES-01", "CV-CLIN-01",
    ]
    assert all(c.matched for c in decision.citations)
    assert decision.missing_fields == []

    # Wire contract — regressing any of these breaks the hackathon compliance
    # story or reintroduces the reasoning-trace-eats-output bug.
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["authorization"] == "Bearer nb-secret"
    body = captured["body"]
    assert body["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    # The user message must carry both intake and rules so the model has
    # everything it needs in one turn.
    user_payload = json.loads(body["messages"][1]["content"])
    assert "intake" in user_payload and "rules" in user_payload
    assert {r["id"] for r in user_payload["rules"]} == {
        "CV-INC-01", "CV-INS-01", "CV-RES-01", "CV-CLIN-01",
    }


@pytest.mark.asyncio
async def test_reason_parses_missing_info_verdict(monkeypatch) -> None:
    """When the model reports a rule can't be evaluated, the Decision must
    carry `missing_info` outcome + populated missing_fields through cleanly."""
    def _ok(_req):
        content = json.dumps({
            "outcome": "missing_info",
            "citations": [
                {"rule_id": "CV-INC-01", "matched": False, "score": None},
            ],
            "missing_fields": ["annual_household_income"],
            "rationale": "Cannot evaluate income rule without household income.",
        })
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    _patch_async_client(monkeypatch, _ok)

    intake = load_canonical_intake()
    decision = await reason(intake, _load_rules()[:1], config=_config())

    assert decision.outcome == "missing_info"
    assert decision.missing_fields == ["annual_household_income"]
    assert decision.citations[0].matched is False


@pytest.mark.asyncio
async def test_reason_raises_on_non_json_content(monkeypatch) -> None:
    def _bad(_req):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "sorry, I can't do that"}}]},
        )

    _patch_async_client(monkeypatch, _bad)

    with pytest.raises(RuntimeError, match="non-JSON"):
        await reason(load_canonical_intake(), _load_rules(), config=_config())


@pytest.mark.asyncio
async def test_reason_raises_on_schema_mismatch(monkeypatch) -> None:
    """JSON that parses but doesn't fit Decision must be caught here, not
    silently propagated to the letter render as a broken Decision."""
    def _bad(_req):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"foo": "bar"})}}]},
        )

    _patch_async_client(monkeypatch, _bad)

    with pytest.raises(RuntimeError, match="Decision schema"):
        await reason(load_canonical_intake(), _load_rules(), config=_config())


@pytest.mark.asyncio
async def test_reason_merges_extras_into_intake_payload(monkeypatch) -> None:
    """Program-specific fields flow to the model through the `extras` seam.

    The reason stage sees Intake as fixed-shape but merges extras (fields
    the program's extraction schema defined that aren't on canonical Intake)
    onto the intake JSON payload — a rule that references `member_id` still
    has something to evaluate against.
    """
    def _ok(_req):
        content = json.dumps({
            "outcome": "approve",
            "citations": [],
            "missing_fields": [],
            "rationale": "ok",
        })
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    captured = _patch_async_client(monkeypatch, _ok)
    await reason(
        load_canonical_intake(),
        _load_rules()[:1],
        config=_config(),
        extras={"member_id": "MC-42", "referring_provider": "Dr. Kim"},
    )
    payload = json.loads(captured["body"]["messages"][1]["content"])
    # Extras appear on the intake blob alongside canonical fields.
    assert payload["intake"]["member_id"] == "MC-42"
    assert payload["intake"]["referring_provider"] == "Dr. Kim"
    # Canonical fields still present — extras don't shadow Intake's dump.
    assert payload["intake"]["patient_name"] == load_canonical_intake().patient_name


@pytest.mark.asyncio
async def test_reason_intake_wins_over_extras_on_name_collision(monkeypatch) -> None:
    """When an extra key collides with an Intake field, the canonical Intake
    value wins — extras are only additive context, not an override channel."""
    def _ok(_req):
        content = json.dumps({
            "outcome": "approve",
            "citations": [],
            "missing_fields": [],
            "rationale": "ok",
        })
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    captured = _patch_async_client(monkeypatch, _ok)
    intake = load_canonical_intake()
    await reason(
        intake,
        _load_rules()[:1],
        config=_config(),
        extras={"patient_name": "Impostor Name"},
    )
    payload = json.loads(captured["body"]["messages"][1]["content"])
    assert payload["intake"]["patient_name"] == intake.patient_name


@pytest.mark.asyncio
async def test_reason_raises_on_http_error(monkeypatch) -> None:
    def _fail(_req):
        return httpx.Response(500, json={"error": "internal"})

    _patch_async_client(monkeypatch, _fail)

    with pytest.raises(httpx.HTTPStatusError):
        await reason(load_canonical_intake(), _load_rules(), config=_config())
