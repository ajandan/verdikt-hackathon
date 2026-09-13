"""Stage 1 (extract) — MiniCPM-V multimodal wire + merge behavior.

The extract stage is the widest surface in the pipeline: many attachments
in, one raw dict out (keyed by the caller's slot names). Per-field
validation is the orchestrator's job — see `_intake_from_extraction`. The
tests here lock the merge semantics and the wire contract; enum/date
validation moved to `test_pap_orchestrator.py`.
"""
from __future__ import annotations

import io
import json
from typing import Any

import httpx
import pytest

from pap_pipeline.stages import extract as extract_mod
from pap_pipeline.stages.extract import ExtractClientConfig, extract


def _config() -> ExtractClientConfig:
    return ExtractClientConfig(
        base_url="https://api.studio.nebius.com/v1",
        api_key="nb-secret",
    )


def _patch_async_client(monkeypatch, handler) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    real_async = httpx.AsyncClient

    def _wrapped(request: httpx.Request) -> httpx.Response:
        calls.append({
            "url": str(request.url),
            "body": json.loads(request.content),
        })
        return handler(request)

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_wrapped)
        return real_async(*args, **kwargs)

    monkeypatch.setattr(extract_mod.httpx, "AsyncClient", _factory)
    return calls


def _png_bytes() -> bytes:
    """Minimal 1x1 PNG — used where the raster path is bypassed (image attachment)."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x9c\x8a\x00\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _one_page_pdf() -> bytes:
    """Real (tiny) PDF via pypdfium2's inverse — actually build one with reportlab
    if available, else use the pdfplumber test fixture path. For unit-test scope
    we just need something starting with %PDF- that pypdfium2 can open."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 720, "Test PAP intake")
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.mark.asyncio
async def test_extract_empty_attachments_yields_empty_dict() -> None:
    """Zero attachments → no vision calls, empty dict (downstream treats
    absent keys as null)."""
    result = await extract([], config=_config())
    assert result == {}


@pytest.mark.asyncio
async def test_extract_calls_vision_model_and_parses_fields(monkeypatch) -> None:
    """Wire contract: /chat/completions, MiniCPM-V model, image_url content
    part, temperature 0, JSON mode, non-reasoning flag. Also asserts the
    dynamically-built prompt names every default slot so the VLM knows the
    schema shape."""
    def _handler(_req):
        content = json.dumps({
            "patient_name": "Maria Chen",
            "date_of_birth": "1978-06-14",
            "household_size": 2,
            "annual_household_income": 60000.00,
            "insurance_status": "uninsured",
            "has_denial_letter": False,
            "country_of_residence": "United States",
            "mailing_state": "California",
            "prescribed_drug": "Cardiovita",
            "prescriber_npi": "1234567893",
            "prescription_date": "2026-08-15",
        })
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    calls = _patch_async_client(monkeypatch, _handler)

    result = await extract([_png_bytes()], config=_config())

    assert result["patient_name"] == "Maria Chen"
    assert result["household_size"] == 2
    assert result["annual_household_income"] == 60000.00
    assert result["insurance_status"] == "uninsured"
    assert result["prescribed_drug"] == "Cardiovita"

    assert len(calls) == 1
    body = calls[0]["body"]
    assert calls[0]["url"].endswith("/chat/completions")
    assert body["model"] == "openbmb/MiniCPM-V-4_5"
    assert body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_object"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # Multimodal message shape.
    parts = body["messages"][0]["content"]
    assert any(p.get("type") == "image_url" for p in parts)
    text_parts = [p for p in parts if p.get("type") == "text"]
    assert text_parts, "extract prompt missing"
    prompt_text = text_parts[0]["text"]
    # Prompt is built from _DEFAULT_SLOTS when the caller passes no slots.
    for name in ("patient_name", "insurance_status", "prescribed_drug"):
        assert f'"{name}"' in prompt_text


@pytest.mark.asyncio
async def test_extract_uses_caller_supplied_slots(monkeypatch) -> None:
    """When the caller passes a program's slot list, the prompt names those
    slots and the returned dict is filtered to just those keys — fields the
    VLM emits outside the program's schema are dropped silently."""
    slots = [
        {"name": "patient_name", "type": "string", "description": "Full name."},
        {"name": "member_id", "type": "string", "description": "Program member ID."},
    ]

    def _handler(_req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "patient_name": "Maria Chen",
            "member_id": "MC-42",
            "prescribed_drug": "Cardiovita",  # not in the caller's slots → dropped
        })}}]})

    calls = _patch_async_client(monkeypatch, _handler)

    result = await extract([_png_bytes()], config=_config(), slots=slots)

    assert result == {"patient_name": "Maria Chen", "member_id": "MC-42"}
    prompt_text = calls[0]["body"]["messages"][0]["content"][0]["text"]
    assert '"member_id"' in prompt_text
    assert '"prescribed_drug"' not in prompt_text


@pytest.mark.asyncio
async def test_extract_merges_partials_first_non_null_wins(monkeypatch) -> None:
    """Two attachments contribute different fields; merged dict carries
    both, and later nulls do NOT overwrite an earlier filled field."""
    responses = [
        {  # pay stub: financial only
            "patient_name": "Maria Chen",
            "annual_household_income": 60000.00,
            "household_size": 2,
        },
        {  # prescription: clinical only, contradicts nothing but names the drug
            "patient_name": None,  # would overwrite if merge were naive
            "prescribed_drug": "Cardiovita",
            "prescriber_npi": "1234567893",
            "prescription_date": "2026-08-15",
        },
    ]

    idx = {"i": 0}

    def _handler(_req):
        body = json.dumps(responses[idx["i"]])
        idx["i"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    _patch_async_client(monkeypatch, _handler)

    result = await extract([_png_bytes(), _png_bytes()], config=_config())
    assert result["patient_name"] == "Maria Chen"  # not overwritten by later null
    assert result["annual_household_income"] == 60000.00
    assert result["prescribed_drug"] == "Cardiovita"


@pytest.mark.asyncio
async def test_extract_ignores_non_json_page(monkeypatch) -> None:
    """A page where the model can't produce JSON contributes nothing but
    doesn't crash the whole extract call."""
    responses = [
        "I couldn't read this document.",  # non-JSON
        json.dumps({"patient_name": "Maria Chen"}),
    ]
    idx = {"i": 0}

    def _handler(_req):
        content = responses[idx["i"]]
        idx["i"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    _patch_async_client(monkeypatch, _handler)

    result = await extract([_png_bytes(), _png_bytes()], config=_config())
    assert result["patient_name"] == "Maria Chen"


@pytest.mark.asyncio
async def test_extract_rasterizes_pdf_to_pages(monkeypatch) -> None:
    """A PDF attachment must be split into pages BEFORE the vision call —
    one call per page, not one call per attachment."""
    pdf = _one_page_pdf()  # 1 page

    def _handler(_req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "patient_name": "Maria Chen",
        })}}]})

    calls = _patch_async_client(monkeypatch, _handler)

    result = await extract([pdf], config=_config())
    assert result["patient_name"] == "Maria Chen"
    assert len(calls) == 1  # one page → one vision call
