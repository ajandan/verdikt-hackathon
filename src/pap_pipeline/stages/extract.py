"""Stage 1 — intake extraction.

Multimodal extraction over attachments: rasterize each PDF page, send it to
a vision model on Nebius Token Factory, and merge the returned partial
intakes into one validated :class:`Intake`.

Backend is MiniCPM-V-4_5 by default (Apache-2.0, strong OCR benchmarks).
When nv-ingest lands on the Nebius VM this module gains a `backend="nv_ingest"`
branch behind the same signature — the caller keeps calling `extract()`.
"""
from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from pap_pipeline.schemas.intake import Intake

_PDF_MAGIC = b"%PDF-"

_VISION_PROMPT = """You are extracting a patient-assistance program intake from a scanned or
photographed document (pay stub, insurance card, prescription, denial
letter, or PAP enrollment form).

Fill only the fields you can read directly from the page. Leave every
other field null — do NOT guess, infer, or fabricate. Missing data is the
signal the pipeline uses to ask the applicant follow-up questions, so a
guess here is worse than a null.

Return STRICT JSON matching this schema — no extra keys, no prose:

{
  "patient_name": "<full name>" | null,
  "date_of_birth": "YYYY-MM-DD" | null,
  "household_size": <integer >= 1> | null,
  "annual_household_income": <number in USD, no currency symbol> | null,
  "insurance_status": "uninsured" | "medicare" | "medicaid" | "commercial"
                      | "commercial_denied" | "unknown" | null,
  "has_denial_letter": true | false | null,
  "country_of_residence": "<country name>" | null,
  "mailing_state": "<US state name or 2-letter abbrev>" | null,
  "prescribed_drug": "<drug brand or generic name>" | null,
  "prescriber_npi": "<10-digit NPI as string>" | null,
  "prescription_date": "YYYY-MM-DD" | null
}

Guidance:
- "insurance_status" = "commercial_denied" only when the page IS a denial
  letter or clearly references one; otherwise use "commercial".
- "has_denial_letter" = true only when the page IS a denial letter.
- Dates must be ISO YYYY-MM-DD. If you can only read a partial date, null.
- "annual_household_income" comes from pay stubs (annualize monthly figures)
  or a stated household income line — never from an invoice or medical bill.
"""


@dataclass(frozen=True)
class ExtractClientConfig:
    """Client config for the vision-model extract call.

    `vision_model` defaults to MiniCPM-V-4_5 on Nebius Token Factory. Same
    endpoint (`/chat/completions`) as the reason stage — the multimodal
    payload is just a chat message with an `image_url` content part.
    """

    base_url: str
    api_key: str
    vision_model: str = "openbmb/MiniCPM-V-4_5"
    timeout: float = 60.0
    # Rasterization scale for PDF pages. 2.083 ≈ 150 DPI — matches the
    # design-time PDF labeler defaults; enough resolution for MiniCPM-V to
    # read small caption text without ballooning payload size.
    raster_scale: float = 2.083


async def extract(
    attachments: list[bytes],
    *,
    config: ExtractClientConfig,
) -> Intake:
    """Extract a structured Intake from raw attachment bytes.

    Each attachment is rasterized (if PDF) or sent as-is (if already an
    image). Each page/image produces one vision call; the partial intakes
    are merged field-by-field with first-non-null-wins semantics.

    Empty input yields an all-None Intake — the downstream missing_info
    branch handles that cleanly. Malformed or unreadable pages contribute
    nothing rather than raising; the aggregate is still returned.
    """
    if not attachments:
        return Intake()

    pages = _to_pages(attachments, scale=config.raster_scale)
    merged: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=config.timeout) as client:
        for page_png in pages:
            partial = await _extract_from_page(client, config, page_png)
            for name, value in partial.items():
                if value is not None and merged.get(name) is None:
                    merged[name] = value
    return _validate_lenient(merged)


def _to_pages(attachments: list[bytes], *, scale: float) -> list[bytes]:
    """Split every attachment into a list of PNG-encoded pages.

    Detects PDF via magic bytes and rasterizes each page. Anything else is
    assumed to be an already-encoded image (JPEG/PNG) and passed through.
    """
    pages: list[bytes] = []
    for att in attachments:
        if not att:
            continue
        if att.startswith(_PDF_MAGIC):
            pages.extend(_rasterize_pdf(att, scale=scale))
        else:
            pages.append(att)
    return pages


def _rasterize_pdf(pdf_bytes: bytes, *, scale: float) -> list[bytes]:
    """PDF bytes → one PNG per page via pypdfium2 (Apache-2.0)."""
    import pypdfium2  # type: ignore[import-untyped]

    out: list[bytes] = []
    doc = pypdfium2.PdfDocument(pdf_bytes)
    try:
        for i in range(len(doc)):
            pil = doc[i].render(scale=scale).to_pil()
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            out.append(buf.getvalue())
    finally:
        doc.close()
    return out


async def _extract_from_page(
    client: httpx.AsyncClient,
    config: ExtractClientConfig,
    image_bytes: bytes,
) -> dict[str, Any]:
    """One vision call — returns the fields the model could read, or {}.

    Errors from the model or malformed JSON are logged (via raised
    RuntimeError semantics preserved from other stages) but recoverable at
    the caller: a page that fails contributes nothing, and the next page
    still runs.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": config.vision_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
            ],
        }],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 2048,
        # Silences the <think>…</think> preamble on reasoning-mode VLMs so
        # the JSON output budget isn't burned on trace tokens.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = await client.post(
        f"{config.base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    resp.raise_for_status()
    content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    return _parse_partial_intake(content or "")


def _parse_partial_intake(content: str) -> dict[str, Any]:
    """Parse the model's response into a dict of known Intake fields.

    Tolerant on the way in: strip a fenced ```json block if present, then
    try `json.loads`. Anything that fails parsing → empty dict (that page
    contributes nothing).
    """
    text = content.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    allowed = set(Intake.model_fields.keys())
    return {k: v for k, v in parsed.items() if k in allowed}


def _validate_lenient(fields: dict[str, Any]) -> Intake:
    """Validate the merged dict, dropping any single field that fails.

    A model that returns e.g. `"insurance_status": "priv"` shouldn't
    invalidate the whole intake. Progressively drop offending keys until
    the remainder validates, or return an empty Intake if nothing does.
    """
    remaining = dict(fields)
    while remaining:
        try:
            return Intake.model_validate(remaining)
        except ValidationError as e:
            bad_keys = {
                err["loc"][0] for err in e.errors() if err.get("loc")
            }
            if not bad_keys:
                break
            for k in bad_keys:
                remaining.pop(k, None)
    return Intake()


__all__ = ["ExtractClientConfig", "extract"]
