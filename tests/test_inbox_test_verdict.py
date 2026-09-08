"""AI verdict scorer behavior for the multi-test executor.

Score_trial returns a pass/fail + one-sentence explanation for each
rendered trial. Tests cover the three paths reviewers care about:

  - render errored → auto-fail, no LLM call (verdict names the error)
  - no AI provider → auto-pass with a "not evaluated" note
  - LLM answers → pass/fail flows through as-is
"""
from __future__ import annotations

import json

import httpx

from docgen.inbox import test_verdict
from docgen.inbox.test_verdict import Verdict, score_trial


def test_render_error_short_circuits_to_fail() -> None:
    """A render_failed trial must not spend a model call to fail it."""
    v = score_trial(
        payload={"foo": "bar"},
        rendered_text="",
        render_error="render_failed: OSError: disk full",
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
    )
    assert isinstance(v, Verdict)
    assert v.passed is False
    assert "render_failed" in v.verdict.lower() or "render failed" in v.verdict.lower()


def test_empty_rendered_text_fails() -> None:
    v = score_trial(
        payload={"foo": "bar"},
        rendered_text="   \n\n",
        render_error=None,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
    )
    assert v.passed is False


def test_no_provider_passes_with_note() -> None:
    """No AI available → don't spuriously fail the trial. Verdict must
    make it obvious no AI actually looked at the output."""
    v = score_trial(
        payload={"foo": "bar"},
        rendered_text="some rendered content",
        render_error=None,
        provider_config=None,
        ai_base_url=None,
        ai_api_key=None,
        ai_model="",
        timeout=1.0,
    )
    assert v.passed is True
    assert "not ai-evaluated" in v.verdict.lower()


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

    v = score_trial(
        payload={"member_name": "Alice"},
        rendered_text="Dear Alice, ...",
        render_error=None,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
    )
    assert v.passed is True
    assert "Alice" in v.verdict or "payload" in v.verdict


def test_schema_reaches_prompt_for_semantic_check(monkeypatch) -> None:
    """When a schema is passed, the field names + types show up in the user
    prompt so the LLM can flag semantic-slot swaps (ZIP in state, etc.)."""
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

    v = score_trial(
        payload={"zip_code": "CA", "state": "94103"},
        rendered_text="ZIP: CA\nState: 94103",
        render_error=None,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
        schema={
            "fields": [
                {"name": "zip_code", "type": "string", "description": "5-digit ZIP"},
                {"name": "state", "type": "string", "description": "2-letter state abbreviation"},
            ]
        },
    )
    # Schema summary must show up in the user prompt so the LLM has context.
    user_msg = captured["body"]["messages"][1]["content"]
    assert "zip_code" in user_msg and "5-digit ZIP" in user_msg
    assert "state" in user_msg and "2-letter" in user_msg
    # The verdict names the swapped fields — this is what the reviewer sees.
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

    v = score_trial(
        payload={"member_name": "Alice"},
        rendered_text="Dear {{ member_name }}, ...",
        render_error=None,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
    )
    assert v.passed is False
    assert "placeholder" in v.verdict.lower()


def test_llm_http_error_passes_with_note(monkeypatch) -> None:
    class _Client:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, *a, **kw):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(test_verdict.httpx, "Client", _Client)

    v = score_trial(
        payload={"member_name": "Alice"},
        rendered_text="Dear Alice, ...",
        render_error=None,
        provider_config={"provider": "nebius"},
        ai_base_url=None,
        ai_api_key="nb-secret",
        ai_model="",
        timeout=1.0,
    )
    # AI unreachable is an infra failure, not a template failure — don't
    # spuriously fail the trial. The note tells the reviewer why.
    assert v.passed is True
    assert "unreachable" in v.verdict.lower() or "not ai" in v.verdict.lower()
