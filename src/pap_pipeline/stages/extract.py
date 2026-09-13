"""Stage 1 — intake extraction.

Multimodal extraction over attachments: rasterize each PDF page, send it to
a vision model on Nebius Token Factory, and merge the returned partial
intakes into one dict keyed by the program's extraction-schema slot names.

Slots come from the Program row's `extraction_schema` (list of dicts, each
{name, type, description, source_field, required, enum_values}). The
extractor builds the vision-model prompt dynamically from those slots — one
JSON key per slot, with the slot's type and description as guidance — so
adding a new field to a Program does NOT require a code change.

Backend is MiniCPM-V-4_5 by default (Apache-2.0, strong OCR benchmarks).
"""
from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

_PDF_MAGIC = b"%PDF-"


# Legacy fallback slots — used when the caller doesn't pass an explicit
# `slots=` argument. Kept in sync with the pap_pipeline.schemas.intake.Intake
# fields so the pre-Program call sites (tests, demo scripts, the
# `run_from_intake` middle-slice) continue to work.
_DEFAULT_SLOTS: list[dict[str, Any]] = [
    {"name": "patient_name", "type": "string", "description": "Patient full legal name as written."},
    {"name": "family_name", "type": "string", "description": "Patient surname / last name only."},
    {"name": "honorific", "type": "enum", "description": "Title (Ms., Mr., Dr.) — only when explicitly written.", "enum_values": ["Mr.", "Ms.", "Mrs.", "Mx.", "Dr."]},  # noqa: E501
    {"name": "date_of_birth", "type": "date", "description": "ISO date; null if partial."},
    {"name": "address_line1", "type": "string", "description": "Street address, first line."},
    {"name": "address_line2", "type": "string", "description": "Apt / unit / suite — null if none."},
    {"name": "city", "type": "string", "description": "City or town."},
    {"name": "postal_code", "type": "string", "description": "ZIP or postal code as written."},
    {"name": "country", "type": "string", "description": "Country name."},
    {"name": "household_size", "type": "integer", "description": "Integer >= 1."},
    {"name": "annual_household_income", "type": "number", "description": "USD, annualize monthly pay stub figures."},  # noqa: E501
    {"name": "insurance_status", "type": "enum", "description": "commercial_denied only when the page IS a denial letter.", "enum_values": ["uninsured", "medicare", "medicaid", "commercial", "commercial_denied", "unknown"]},  # noqa: E501
    {"name": "has_denial_letter", "type": "boolean", "description": "True only when the page IS a denial letter."},  # noqa: E501
    {"name": "country_of_residence", "type": "string", "description": "Country of residence."},
    {"name": "mailing_state", "type": "string", "description": "US state name or 2-letter abbrev. Leave null for non-US applicants."},  # noqa: E501
    {"name": "prescribed_drug", "type": "string", "description": "Drug brand or generic name."},
    {"name": "prescriber_npi", "type": "string", "description": "10-digit NPI as string."},
    {"name": "prescription_date", "type": "date", "description": "ISO date."},
    {"name": "referral_id", "type": "string", "description": "Form-supplied application/reference number. Do not invent."},  # noqa: E501
]


_PROMPT_HEADER = """You are extracting a patient-assistance program intake from a scanned or
photographed document (pay stub, insurance card, prescription, denial
letter, or PAP enrollment form).

Fill only the fields you can read directly from the page. Leave every
other field null — do NOT guess, infer, or fabricate. Missing data is the
signal the pipeline uses to ask the applicant follow-up questions, so a
guess here is worse than a null.

Return STRICT JSON matching this schema — no extra keys, no prose:
"""


_PROMPT_FOOTER = """
Guidance:
- Dates must be ISO YYYY-MM-DD. If you can only read a partial date, null.
- Numbers must be plain (no currency symbols, no thousands separators).
- Enum fields must exactly match one of the listed values (case-sensitive).
- Never fabricate IDs, reference numbers, or names. When in doubt, null.
- Never stuff a non-US region into a US-state slot.
"""


@dataclass(frozen=True)
class ExtractClientConfig:
    """Client config for the vision-model extract call.

    `vision_model` defaults to MiniCPM-V-4_5 on Nebius Token Factory.
    """

    base_url: str
    api_key: str
    vision_model: str = "openbmb/MiniCPM-V-4_5"
    timeout: float = 60.0
    # Rasterization scale for PDF pages. 2.083 ≈ 150 DPI — enough resolution
    # for MiniCPM-V to read small caption text without ballooning payload.
    raster_scale: float = 2.083


def _slot_schema_line(slot: dict[str, Any]) -> str:
    """Render one slot as a JSON schema line for the VLM prompt.

    Type → JSON hint mapping:
      string   → "<value>" | null
      date     → "YYYY-MM-DD" | null
      integer  → <integer> | null
      number   → <number> | null
      boolean  → true | false | null
      enum     → "value1" | "value2" | ... | null
    """
    name = slot["name"]
    typ = slot.get("type", "string")
    desc = slot.get("description") or ""

    if typ == "date":
        hint = '"YYYY-MM-DD" | null'
    elif typ == "integer":
        hint = "<integer> | null"
    elif typ == "number":
        hint = "<number> | null"
    elif typ == "boolean":
        hint = "true | false | null"
    elif typ == "enum":
        vals = slot.get("enum_values") or []
        hint = " | ".join(f'"{v}"' for v in vals) + " | null" if vals else '"<value>" | null'
    else:
        hint = '"<value>" | null'

    return f'  "{name}": {hint},' + (f"  // {desc}" if desc else "")


def _build_prompt(slots: list[dict[str, Any]]) -> str:
    """Assemble the full VLM prompt from a list of slot dicts."""
    lines = [_slot_schema_line(s) for s in slots]
    # Trim the trailing comma on the last line — strict JSON.
    if lines:
        base_line = lines[-1]
        comment_split = base_line.find("//")
        if comment_split >= 0:
            head, tail = base_line[:comment_split].rstrip(), base_line[comment_split:]
            if head.endswith(","):
                head = head[:-1]
            lines[-1] = head + "  " + tail
        else:
            if base_line.endswith(","):
                lines[-1] = base_line[:-1]
    body = "\n".join(lines)
    return f"{_PROMPT_HEADER}\n{{\n{body}\n}}\n{_PROMPT_FOOTER}"


async def extract(
    attachments: list[bytes],
    *,
    config: ExtractClientConfig,
    slots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract a structured intake dict from raw attachment bytes.

    `slots` is the program's extraction schema — a list of slot dicts. When
    omitted, `_DEFAULT_SLOTS` is used (matches the legacy Intake shape) so
    pre-Program call sites keep working.

    Each attachment is rasterized (if PDF) or sent as-is (if already an
    image). Each page/image produces one vision call; the partial dicts
    are merged field-by-field with first-non-null-wins semantics.

    Empty input yields an empty dict — downstream missing_info handling
    treats absent keys as null.
    """
    active_slots = slots or _DEFAULT_SLOTS
    if not attachments:
        return {}

    prompt = _build_prompt(active_slots)
    allowed_names = {s["name"] for s in active_slots}

    pages = _to_pages(attachments, scale=config.raster_scale)
    merged: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=config.timeout) as client:
        for page_png in pages:
            partial = await _extract_from_page(client, config, page_png, prompt, allowed_names)
            for name, value in partial.items():
                if value is not None and merged.get(name) is None:
                    merged[name] = value
    return merged


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
    prompt: str,
    allowed_names: set[str],
) -> dict[str, Any]:
    """One vision call — returns the fields the model could read, or {}."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": config.vision_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
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
    return _parse_partial_intake(content or "", allowed_names)


def _parse_partial_intake(content: str, allowed_names: set[str]) -> dict[str, Any]:
    """Parse the model's response into a dict of known slot values.

    Tolerant on the way in: strip a fenced ```json block if present, then
    try `json.loads`. Anything that fails parsing → empty dict.
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
    return {k: v for k, v in parsed.items() if k in allowed_names}


__all__ = ["ExtractClientConfig", "extract"]
