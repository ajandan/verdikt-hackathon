"""Ad-hoc quality check for Path A: text-only Nemotron Super labeling on top of
geometric field detection. No VLM in the loop.

Steps:
  1. Run the existing `_drawings_to_fields` geometric detector on the flat and
     acroform demo PDFs. Report what geometry alone finds.
  2. For each detected/widget field rect, extract nearby OCR text spans and
     send the whole set to nvidia/nemotron-3-super-120b-a12b with a strict-JSON
     prompt. Print labels + section for every field so we can judge quality.

Run: python scripts/qc_nemotron_labeling.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
import pymupdf  # type: ignore[import-untyped]

from docgen.config import get_settings
from docgen.designtime.pdf_field_detector import (
    _drawings_to_fields,
    _detect_cells,
    _geometric_labels_for_widgets,
)


ROOT = Path(__file__).resolve().parent.parent
FLAT_PDF = ROOT / "sample_templates" / "denial_letter_flat.pdf"
ACROFORM_PDF = ROOT / "sample_templates" / "denial_letter_acroform.pdf"

MODEL = "nvidia/nemotron-3-super-120b-a12b"
BASE_URL = "https://api.studio.nebius.com/v1"


EXPECTED_ACROFORM_LABELS = {
    "letter_date": "Date",
    "member_name": "Dear (patient/member name)",
    "claim_id": "claim number (We have reviewed claim ___)",
    "claim_date": "submitted on (date)",
    "amount": "amount (US$)",
    "denial_reason": "Reason for denial",
    "appeal_phone": "Contact us at (phone)",
    "agent_name": "signature / agent name",
}


def extract_text_lines(pdf_path: Path) -> list[list[dict]]:
    """Return per-page list of text lines with rects, sorted top-to-bottom.
    Each line: {'text': str, 'x0','y0','x1','y1'}."""
    out: list[list[dict]] = []
    with pymupdf.open(str(pdf_path)) as doc:
        for page in doc:
            td = page.get_text("dict")
            lines_out: list[dict] = []
            for block in td.get("blocks", []):
                for line in block.get("lines", []):
                    bbox = line.get("bbox")
                    if not bbox:
                        continue
                    txt = "".join((s.get("text") or "") for s in line.get("spans", []))
                    txt = txt.strip()
                    if not txt:
                        continue
                    lines_out.append({
                        "text": txt,
                        "x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3],
                    })
            lines_out.sort(key=lambda l: (l["y0"], l["x0"]))
            out.append(lines_out)
    return out


def geometric_detect_flat(pdf_path: Path) -> list[dict]:
    """Run the same geometric pass as production for a flat PDF, per page."""
    fields: list[dict] = []
    with pymupdf.open(str(pdf_path)) as doc:
        for page_num, page in enumerate(doc, 1):
            text_chars = sum(
                len((s.get("text") or "").strip())
                for b in page.get_text("dict").get("blocks", [])
                for line in b.get("lines", [])
                for s in line.get("spans", [])
            )
            if text_chars == 0:
                cells = _detect_cells(page)
                for f in cells:
                    fields.append({"page": page_num, **f})
                continue
            geo = _drawings_to_fields(page)
            for f in geo:
                fields.append({"page": page_num, **f})
    return fields


def acroform_widgets(pdf_path: Path) -> list[dict]:
    fields: list[dict] = []
    with pymupdf.open(str(pdf_path)) as doc:
        for page_num, page in enumerate(doc, 1):
            pw, ph = page.rect.width, page.rect.height
            for i, w in enumerate(page.widgets() or []):
                r = w.rect
                fields.append({
                    "index": i,
                    "name": w.field_name or f"widget_{i}",
                    "page": page_num,
                    "x_pct": r.x0 / pw,
                    "y_pct": r.y0 / ph,
                    "w_pct": (r.x1 - r.x0) / pw,
                    "h_pct": (r.y1 - r.y0) / ph,
                    "field_type": "checkbox" if w.field_type_string == "CheckBox" else "text",
                })
    return fields


PROMPT = """You are labeling fillable fields on a PDF form using positional coordinates and OCR text — NO image is provided.

Each field is a blank rectangle where a user would type a value. You must pick the printed caption that describes THIS specific field.

Rules for picking `label`:
- The caption is USUALLY the printed text immediately to the LEFT of the field on the same row (same y), OR immediately ABOVE the field in the same x-column.
- Prefer a short caption phrase (e.g. "First Name", "Date", "US$"), not a whole sentence.
- If the field sits after a phrase like "for the amount of US$ ___", the caption is "amount" (or the noun the phrase is asking for), not the entire sentence.
- Use the OCR text verbatim for wording.
- If nothing sensible fits, return "" for label.

Rules for `section`:
- The nearest heading printed above the field. Empty string if no heading applies.

--- OCR text lines for page {page_num} (top-to-bottom; each line is `y=<y0> x=<x0..x1>: <text>`) ---
{ocr_dump}
--- END OCR ---

Fields to label on page {page_num}. Each is `#<idx> at (x=<x0..x1>, y=<y0..y1>) [type]`:
{fields_dump}

Return STRICT JSON:
{{"labels": [{{"index": 0, "section": "", "label": "..."}}, ...]}}

Every listed index must appear exactly once.
"""


def build_ocr_dump(lines: list[dict]) -> str:
    return "\n".join(
        f"y={l['y0']:.0f} x={l['x0']:.0f}..{l['x1']:.0f}: {l['text']}"
        for l in lines
    )


def build_fields_dump(fields: list[dict], page_w: float, page_h: float) -> str:
    out = []
    for i, f in enumerate(fields):
        x0 = f["x_pct"] * page_w
        y0 = f["y_pct"] * page_h
        x1 = x0 + f["w_pct"] * page_w
        y1 = y0 + f["h_pct"] * page_h
        ftype = f.get("field_type") or "text"
        out.append(f"#{i} at (x={x0:.0f}..{x1:.0f}, y={y0:.0f}..{y1:.0f}) [{ftype}]")
    return "\n".join(out)


async def label_with_nemotron(
    api_key: str, page_num: int, page_w: float, page_h: float,
    lines: list[dict], fields: list[dict],
) -> dict[int, dict[str, str]]:
    prompt = PROMPT.format(
        page_num=page_num,
        ocr_dump=build_ocr_dump(lines),
        fields_dump=build_fields_dump(fields, page_w, page_h),
    )
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    # Nebius wraps `extra_body` at top level; keep it there:
    extra = body.pop("extra_body")
    body.update(extra)
    async with httpx.AsyncClient(timeout=180.0) as client:
        t0 = time.time()
        resp = await client.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        dt = time.time() - t0
    if resp.status_code >= 400:
        print(f"  !! Nemotron {resp.status_code}: {resp.text[:400]}")
        return {}
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except Exception:
        # Nemotron sometimes wraps JSON in a code fence; strip it.
        stripped = content.strip().strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
        parsed = json.loads(stripped)
    labels = {}
    for row in parsed.get("labels", []):
        idx = int(row.get("index"))
        labels[idx] = {
            "label": str(row.get("label") or ""),
            "section": str(row.get("section") or ""),
        }
    print(f"  Nemotron labeled {len(labels)}/{len(fields)} in {dt:.1f}s")
    return labels


async def run_one(name: str, pdf_path: Path, fields_all: list[dict], api_key: str):
    print(f"\n{'='*70}\n{name}: {pdf_path.name} — {len(fields_all)} fields via geometry\n{'='*70}")
    if not fields_all:
        print("  (no fields)")
        return
    # Group by page.
    lines_by_page = extract_text_lines(pdf_path)
    with pymupdf.open(str(pdf_path)) as doc:
        by_page: dict[int, list[dict]] = {}
        for f in fields_all:
            by_page.setdefault(int(f["page"]), []).append(f)
        for page_num, page_fields in by_page.items():
            page = doc.load_page(page_num - 1)
            labels = await label_with_nemotron(
                api_key,
                page_num=page_num,
                page_w=float(page.rect.width),
                page_h=float(page.rect.height),
                lines=lines_by_page[page_num - 1],
                fields=page_fields,
            )
            for i, f in enumerate(page_fields):
                lab = labels.get(i, {})
                x0 = f["x_pct"] * page.rect.width
                y0 = f["y_pct"] * page.rect.height
                marker = ""
                if "name" in f:
                    marker = f"  [truth: name={f['name']!r}]"
                print(
                    f"  #{i:>2} p{page_num} pos=({x0:.0f},{y0:.0f}) "
                    f"section={lab.get('section','')!r:<30} label={lab.get('label','')!r}{marker}"
                )


async def main() -> None:
    settings = get_settings()
    api_key = settings.ai_api_key
    assert api_key, "DOCGEN_AI_API_KEY must be set"
    print(f"model={MODEL}  base={BASE_URL}")

    # Flat PDF: geometry-only detection, then Nemotron labeling.
    flat_fields = geometric_detect_flat(FLAT_PDF)
    print(f"\nflat PDF: geometry detected {len(flat_fields)} fields")
    for i, f in enumerate(flat_fields):
        print(f"  #{i} p{f['page']} x={f['x_pct']:.3f} y={f['y_pct']:.3f} w={f['w_pct']:.3f}")

    await run_one("FLAT", FLAT_PDF, flat_fields, api_key)

    # AcroForm PDF: real widgets + Nemotron labels + reference names.
    acro_fields = acroform_widgets(ACROFORM_PDF)
    print(f"\nacroform PDF: {len(acro_fields)} widgets")
    await run_one("ACROFORM", ACROFORM_PDF, acro_fields, api_key)


if __name__ == "__main__":
    asyncio.run(main())
