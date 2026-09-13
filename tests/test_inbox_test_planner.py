"""AI test-data planner behavior.

The planner drives the multi-test executor: N labeled payloads per
ingest, each validated against the frozen schema. These tests cover the
three modes it can be in:

  - no AI configured → deterministic synth fallback (5 rows, distinct labels)
  - AI returns clean JSON → those payloads flow through, count is honored
  - AI returns garbage or fails → fall back to synth for the affected rows
"""
from __future__ import annotations

import json

import httpx
import pytest

from docgen.inbox import test_planner
from docgen.inbox.test_planner import Trial, plan_trials

_SCHEMA = {
    "fields": [
        {"name": "member_name", "type": "string"},
        {"name": "amount", "type": "integer"},
        {"name": "is_denied", "type": "boolean"},
    ]
}


def test_no_provider_falls_back_to_synth() -> None:
    trials = plan_trials(
        _SCHEMA,
        provider_config=None,
        ai_base_url=None,
        ai_api_key=None,
        ai_model="",
        timeout=1.0,
        count=5,
    )
    assert len(trials) == 5
    # Deterministic fallback labels — reviewers can still tell the rows apart
    # even without AI, so the summary table doesn't read "Test 1..5" of empty.
    names = [t.name for t in trials]
    assert names[0] == "happy path"
    assert len(set(names)) >= 3  # multiple distinct labels


def test_stub_provider_name_uses_fallback() -> None:
    # provider="stub" (or any unknown) short-circuits — never hits httpx.
    trials = plan_trials(
        _SCHEMA,
        provider_config={"provider": "stub"},
        ai_base_url="http://ignored",
        ai_api_key="ignored",
        ai_model="ignored",
        timeout=1.0,
        count=3,
    )
    assert len(trials) == 3
    assert all(isinstance(t, Trial) for t in trials)


def test_ai_response_flows_through(monkeypatch) -> None:
    class _FakeResp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        def __init__(self, *a, **kw) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, url, headers, json):
            content = json_dumps({
                "trials": [
                    {"name": "happy",  "description": "typical", "payload": {"member_name": "Alice", "amount": 1, "is_denied": False}},  # noqa: E501
                    {"name": "edge",   "description": "boundary", "payload": {"member_name": "B",     "amount": 999, "is_denied": True}},  # noqa: E501
                ]
            })
            return _FakeResp({"choices": [{"message": {"content": content}}]})

    def json_dumps(obj):
        return json.dumps(obj)

    monkeypatch.setattr(test_planner.httpx, "Client", _FakeClient)

    trials = plan_trials(
        _SCHEMA,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=5.0,
        count=2,
    )
    assert [t.name for t in trials] == ["happy", "edge"]
    assert trials[0].payload["member_name"] == "Alice"


def test_ai_returns_invalid_payload_falls_back_per_row(monkeypatch) -> None:
    """One malformed payload → that trial gets synth values; others survive."""

    class _FakeResp:
        def raise_for_status(self) -> None: return None
        def json(self) -> dict:
            content = json.dumps({
                "trials": [
                    {"name": "good", "description": "ok",
                     "payload": {"member_name": "Alice", "amount": 5, "is_denied": False}},
                    # Missing 'amount' would fail schema validation.
                    {"name": "bad",  "description": "schema violation",
                     "payload": {"member_name": "Bob", "is_denied": True}},
                ]
            })
            return {"choices": [{"message": {"content": content}}]}

    class _FakeClient:
        def __init__(self, *a, **kw) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, *a, **kw): return _FakeResp()

    monkeypatch.setattr(test_planner.httpx, "Client", _FakeClient)

    trials = plan_trials(
        {**_SCHEMA, "fields": [
            {"name": "member_name", "type": "string", "required": True},
            {"name": "amount", "type": "integer", "required": True},
            {"name": "is_denied", "type": "boolean", "required": True},
        ]},
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=5.0,
        count=2,
    )
    assert trials[0].name == "good"
    assert trials[0].payload["member_name"] == "Alice"
    # Bad payload rebuilt with fallback so every trial has a complete payload.
    assert trials[1].name == "bad"
    assert "amount" in trials[1].payload
    assert isinstance(trials[1].payload["amount"], int)


def test_http_error_falls_back(monkeypatch) -> None:
    class _FakeClient:
        def __init__(self, *a, **kw) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, *a, **kw):
            raise httpx.ConnectError("no route")

    monkeypatch.setattr(test_planner.httpx, "Client", _FakeClient)

    trials = plan_trials(
        _SCHEMA,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
        count=5,
    )
    # Should not raise — falls back to synth for the full requested count.
    assert len(trials) == 5


def test_count_is_honored_when_ai_returns_fewer_trials(monkeypatch) -> None:
    """AI hands back 2 rows but caller asked for 5 → last 3 come from synth."""

    class _FakeClient:
        def __init__(self, *a, **kw) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, *a, **kw):
            content = json.dumps({"trials": [
                {"name": "one", "description": "",
                 "payload": {"member_name": "A", "amount": 1, "is_denied": False}},
                {"name": "two", "description": "",
                 "payload": {"member_name": "B", "amount": 2, "is_denied": True}},
            ]})
            return _Resp(content)

    class _Resp:
        def __init__(self, content): self._c = content
        def raise_for_status(self): return None
        def json(self): return {"choices": [{"message": {"content": self._c}}]}

    monkeypatch.setattr(test_planner.httpx, "Client", _FakeClient)

    trials = plan_trials(
        _SCHEMA,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
        count=5,
    )
    assert len(trials) == 5
    assert [t.name for t in trials[:2]] == ["one", "two"]
    # Remaining 3 fallback rows get placeholder labels like "Test 3", "Test 4".
    for i in (2, 3, 4):
        assert trials[i].name.startswith("Test ")
