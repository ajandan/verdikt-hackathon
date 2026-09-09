"""AI verdict scorer behavior for the multi-test executor.

Score_trial returns a pass/fail + one-sentence explanation for each
rendered trial. Tests cover the reviewer-visible paths:

  - render errored → auto-fail, no LLM call (verdict names the error)
  - no AI provider / unreachable → auto-fail with a note (used to be an
    auto-pass; that silently inflated scores when reasoning models
    returned empty content, so unreachable-model trials now fail loudly)
  - LLM answers → pass/fail flows through as-is

These tests exercise the DOCX (text) path. The PDF path uses the vision
model with page-image content parts; that's covered by the smoke test
against a rendered blob rather than a unit test to avoid mocking the
image encoder + vision endpoint.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from docgen.inbox import test_verdict
from docgen.inbox.test_verdict import Verdict, score_trial


def _call(**overrides):
    """Default DOCX-path kwargs so each test only sets what it exercises."""
    kwargs = dict(
        payload={"foo": "bar"},
        rendered=None,
        rendered_text="some rendered content",
        ext="docx",
        render_error=None,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
    )
    kwargs.update(overrides)
    return score_trial(**kwargs)


def test_render_error_short_circuits_to_fail() -> None:
    v = _call(rendered_text="", render_error="render_failed: OSError: disk full")
    assert isinstance(v, Verdict)
    assert v.passed is False
    assert "render_failed" in v.verdict.lower() or "render failed" in v.verdict.lower()


def test_empty_rendered_text_fails() -> None:
    v = _call(rendered_text="   \n\n")
    assert v.passed is False


def test_no_provider_fails_with_note() -> None:
    """No AI available → fail the trial loudly. Old behavior was auto-pass
    with a note, but that silently inflated scores whenever the model
    returned empty content."""
    v = _call(provider_config=None, ai_api_key=None)
    assert v.passed is False
    assert "manual review" in v.verdict.lower() or "unavailable" in v.verdict.lower()


def test_llm_response_flows_through(monkeypatch) -> None:
    class _Resp:
        def raise_for_status(self): return None
        def json(self):
            content = json.dumps({
                "passed": True,
                "verdict": "All payload values appear in the rendered output.",
            })
            return {"choices": [{"message": {"content": content}}]}

    class _Client:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr(test_verdict.httpx, "Client", _Client)

    v = _call(payload={"member_name": "Alice"}, rendered_text="Dear Alice, ...")
    assert v.passed is True
    assert "Alice" in v.verdict or "payload" in v.verdict


def test_schema_reaches_prompt_for_semantic_check(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self): return None
        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "passed": False,
                "verdict": "zip_code and state appear swapped.",
            })}}]}

    class _Client:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, url, headers, json):  # noqa: A002
            captured["body"] = json
            return _Resp()

    monkeypatch.setattr(test_verdict.httpx, "Client", _Client)

    v = _call(
        payload={"zip_code": "CA", "state": "94103"},
        rendered_text="ZIP: CA\nState: 94103",
        schema={
            "fields": [
                {"name": "zip_code", "type": "string", "description": "5-digit ZIP"},
                {"name": "state", "type": "string", "description": "2-letter state abbreviation"},
            ]
        },
    )
    user_msg = captured["body"]["messages"][1]["content"]
    assert "zip_code" in user_msg and "5-digit ZIP" in user_msg
    assert "state" in user_msg and "2-letter" in user_msg
    assert v.passed is False
    assert "zip_code" in v.verdict and "state" in v.verdict


def test_llm_says_fail(monkeypatch) -> None:
    class _Resp:
        def raise_for_status(self): return None
        def json(self):
            content = json.dumps({
                "passed": False,
                "verdict": "Placeholder tokens still visible in output.",
            })
            return {"choices": [{"message": {"content": content}}]}

    class _Client:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr(test_verdict.httpx, "Client", _Client)

    v = _call(
        payload={"member_name": "Alice"},
        rendered_text="Dear {{ member_name }}, ...",
    )
    assert v.passed is False
    assert "placeholder" in v.verdict.lower()


def test_llm_http_error_fails_with_note(monkeypatch) -> None:
    """AI unreachable now fails the trial. Reviewers see the reason and
    can retry manually — better than silently green-lighting the trial."""
    class _Client:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, *a, **kw):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(test_verdict.httpx, "Client", _Client)

    v = _call(payload={"member_name": "Alice"}, rendered_text="Dear Alice, ...")
    assert v.passed is False
    assert "unreachable" in v.verdict.lower() or "unavailable" in v.verdict.lower()
