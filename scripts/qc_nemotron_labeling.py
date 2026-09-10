"""Ad-hoc quality check for Path A: text-only Nemotron Super labeling on top of
geometric field detection. No VLM in the loop.

Steps:
  1. Run the production geometric detector (`detect_fields_in_pdf`) on the
     flat demo PDF. Report what geometry alone finds.
  2. Harvest AcroForm widgets from the acroform demo PDF via pypdf so we
     have field names + widget rects.
  3. For each detected/widget field, extract nearby OCR text spans and
     send the whole set to nvidia/nemotron-3-super-120b-a12b with a strict
     JSON prompt. Print labels + section for every field so we can judge
     quality.

Run: python scripts/qc_nemotron_labeling.py
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
import pdfplumber  # type: ignore[import-untyped]
import pypdf

from docgen.config import get_settings
from docgen.designtime.pdf_field_detector import detect_fields_in_pdf
from docgen.designtime.pdf_text import extract_words, group_into_visual_rows
from docgen.designtime.preflight_pdf import _harvest_widget_placements


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


def extract_text_lines(pdf_path: Path) -> list[list[dict[str, Any]]]:
    """Return per-page list of text lines with rects, sorted top-to-bottom.

    Uses the shared `extract_words` + `group_into_visual_rows` primitive
    from `docgen.designtime.pdf_text`, then flattens each visual row into
    one dict `{text, x0, y0, x1, y1}` matching the historical shape.
    """
    out: list[list[dict[str, Any]]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            lines_out: list[dict[str, Any]] = []
            words = extract_words(page)
            for row in group_into_visual_rows(words):
                if not row:
                    continue
                text = " ".join(w.text for w in row).strip()
                if not text:
                    continue
                x0 = min(w.rect.x0 for w in row)
                x1 = max(w.rect.x1 for w in row)
                y0 = min(w.rect.y0 for w in row)
                y1 = max(w.rect.y1 for w in row)
                lines_out.append({
                    "text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                })
            lines_out.sort(key=lambda ln: (ln["y0"], ln["x0"]))
            out.append(lines_out)
    return out


def _page_sizes(pdf_path: Path) -> list[tuple[float, float]]:
    with pdfplumber.open(str(pdf_path)) as pdf:
        return [(float(p.width), float(p.height)) for p in pdf.pages]


async def geometric_detect_flat(pdf_path: Path) -> list[dict[str, Any]]:
    """Run production geometric detection against a flat PDF."""
    data = pdf_path.read_bytes()
    return await detect_fields_in_pdf(data)


def acroform_widgets(pdf_path: Path) -> list[dict[str, Any]]:
    """Enumerate AcroForm widgets via pypdf's harvest helper.

    Emits the same shape production uses — placements are dicts with
    `page`, `x_pct`, `y_pct`, `w_pct`, `h_pct`, `field_type`, `name`.
    We only need a subset here (rendering `#i at (x=..)` markers), so the
    extra keys are just carried through.
    """
    with pdf_path.open("rb") as f:
        reader = pypdf.PdfReader(f)
        placements = _harvest_widget_placements(reader)
    fields: list[dict[str, Any]] = []
    for i, p in enumerate(placements):
        p = dict(p)
        p["index"] = i
        fields.append(p)
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


def build_ocr_dump(lines: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"y={l['y0']:.0f} x={l['x0']:.0f}..{l['x1']:.0f}: {l['text']}"
        for l in lines
    )


def build_fields_dump(
    fields: list[dict[str, Any]], page_w: float, page_h: float
) -> str:
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
    lines: list[dict[str, Any]], fields: list[dict[str, Any]],
) -> dict[int, dict[str, str]]:
    prompt = PROMPT.format(
        page_num=page_num,
        ocr_dump=build_ocr_dump(lines),
        fields_dump=build_fields_dump(fields, page_w, page_h),
    )
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
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


async def run_one(
    name: str, pdf_path: Path, fields_all: list[dict[str, Any]], api_key: str
) -> None:
    print(f"\n{'='*70}\n{name}: {pdf_path.name} — {len(fields_all)} fields via geometry\n{'='*70}")
    if not fields_all:
        print("  (no fields)")
        return
    lines_by_page = extract_text_lines(pdf_path)
    sizes = _page_sizes(pdf_path)
    by_page: dict[int, list[dict[str, Any]]] = {}
    for f in fields_all:
        by_page.setdefault(int(f["page"]), []).append(f)
    for page_num, page_fields in by_page.items():
        pw, ph = sizes[page_num - 1]
        labels = await label_with_nemotron(
            api_key,
            page_num=page_num,
            page_w=pw,
            page_h=ph,
            lines=lines_by_page[page_num - 1],
            fields=page_fields,
        )
        for i, f in enumerate(page_fields):
            lab = labels.get(i, {})
            x0 = f["x_pct"] * pw
            y0 = f["y_pct"] * ph
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
    flat_fields = await geometric_detect_flat(FLAT_PDF)
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
