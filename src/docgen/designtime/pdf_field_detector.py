"""Mistral OCR + vision field labeler for PDF templates.

Per page:
  1. `/v1/ocr` (mistral-ocr-latest) gives an accurate markdown transcript.
  2. That transcript, plus a rendered PNG of the page with red rectangles
     and yellow "#N" tags burned onto every field widget, is sent to a
     Mistral vision chat model (pixtral-12b-2409). The model matches each
     numbered widget to the caption that sits next to it — picking the
     wording from the OCR transcript so it doesn't have to visually read
     small print, and using the image only for spatial grounding.

Returns `{index: {"label": ..., "section": ...}}`. Section is the field's
group heading on the page ("Patient Information Section", etc.), which the
UI slugs into `section_label` payload keys to disambiguate captions that
repeat under different headings.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

import httpx

_BATCH_SIZE = 20
_BATCH_CONCURRENCY = 4
_MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
# Rasterization scale for the annotated page image (2x native ~ 1700x2200
# on US-letter — enough resolution for the model to read small captions
# without blowing up payload size).
_RENDER_SCALE = 2.0


_PROMPT_TEMPLATE = """You are labeling fillable fields on PAGE {page_num} of a PDF form.

I am giving you two things about this single page:
  1. An IMAGE of the page with every field marked by a short horizontal RED
     BAR (a text field's blank underline) or a RED SQUARE (a checkbox).
     Each marker has a numeric tag like "#3" in a YELLOW box positioned
     immediately to its LEFT (or to its right if flush against the left
     margin). The tag's vertical centre is aligned with the marker.
  2. The full TEXT of the page as extracted by OCR (markdown format —
     headings preserved). This transcript is authoritative for wording;
     use it verbatim when picking captions.

For each numbered marker listed below, return:
  - `section` — the section heading the field belongs to (the nearest
    markdown H1/H2/bold heading printed ABOVE the field, verbatim from the
    OCR text). Empty string if the field is at the top of the page with no
    heading above it.
  - `label` — the short caption printed next to THIS specific marker:
    * Text-field red BAR: caption is the printed text sitting DIRECTLY
      ABOVE the bar (same column) or IMMEDIATELY TO THE LEFT of the bar
      on the same line (e.g. "First Name", "ZIP Code", "Date of Birth").
    * Checkbox red SQUARE: caption is the option text IMMEDIATELY TO THE
      RIGHT of the square (e.g. "Medicare Part D", "Male").
    Never take the caption from the row above the marker's row — that
    caption belongs to a different field.

Indices to label on this page: {index_list}

--- BEGIN PAGE OCR TEXT ---
{ocr_text}
--- END PAGE OCR TEXT ---

Return STRICT JSON exactly matching:
{{"labels": [{{"index": 1, "section": "Patient Information Section", "label": "First Name"}}, ...]}}

Rules:
- Every listed INDEX must appear exactly once.
- Section and label must match the OCR text verbatim (case, punctuation).
- If no legible caption sits next to the rectangle, return an empty string
  for `label`.
- No text outside the JSON object."""


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first balanced `{...}` block from the model's response.

    A greedy `\\{.*\\}` regex breaks when the model wraps JSON in prose
    ('Here is the answer: {...}. Let me know if...') or emits multiple
    objects — the greedy match spans them all and json.loads chokes at a
    random offset. Bracket-counting is cheap and correct.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    start = stripped.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(stripped[start : i + 1])
    raise ValueError(f"unbalanced JSON in response: {text[:200]!r}")


def _annotate_and_render_page(
    pdf_bytes: bytes,
    page_num: int,
    fields: list[dict[str, Any]],
    *,
    scale: float = _RENDER_SCALE,
    pdf_flavor: str | None = None,
) -> bytes:
    """Render page `page_num` (1-based) to PNG with each field outlined and
    numbered by its batch `index`.

    Annotations are drawn on a per-call copy of the source PDF (loaded from
    bytes) using pymupdf's own draw primitives, so the caller's PDF is
    untouched and we don't need Pillow.

    Text-field marker geometry differs by flavor:
      * flat: the placement rect is a detected underline shifted 14pt UP
        (see _UNDERLINE_UP in detector) so the renderer can lay values on
        top of the line. Drawing the whole rect would cover the caption
        printed above the underline, and tagging it puts the number next
        to the previous row's caption — the model then labels every field
        with the wrong row. So we draw a thin marker AT the underline
        (bottom edge only) and put the tag to its left.
      * acroform: the placement rect IS the real widget /Rect — the
        visible input box the user clicks on. Outlining the whole box
        gives the model much stronger spatial grounding (especially on
        pages with duplicated captions like "Address / City / State"
        appearing in multiple sub-blocks). A thin bottom bar on a 20pt
        widget would drop the model back to guessing.
    """
    import pymupdf  # type: ignore[import-untyped]

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if page_num < 1 or page_num > doc.page_count:
            raise ValueError(
                f"page {page_num} out of range for {doc.page_count}-page PDF"
            )
        page = doc.load_page(page_num - 1)
        page_w = float(page.rect.width)
        page_h = float(page.rect.height)
        tag_w, tag_h = 22.0, 11.0
        for f in fields:
            try:
                idx = int(f["index"])
                x0 = float(f.get("x_pct") or 0) * page_w
                y0 = float(f.get("y_pct") or 0) * page_h
                w = float(f.get("w_pct") or 0) * page_w
                h_pct = float(f.get("h_pct") or 0)
                h = h_pct * page_h if h_pct > 0 else 12.0
            except (KeyError, TypeError, ValueError):
                continue
            x1 = min(page_w, x0 + w)
            y1 = min(page_h, y0 + h)
            ftype = str(f.get("field_type") or "text").lower()

            if ftype == "checkbox":
                # Checkbox rects are the actual visible widget — outline them
                # in place. Tag goes just LEFT of the box at its vertical
                # centre so it doesn't obscure the caption sitting to the
                # right.
                rect = pymupdf.Rect(x0, y0, x1, y1)
                page.draw_rect(rect, color=(1, 0, 0), width=1.5, overlay=True)
                anchor_y = (y0 + y1) / 2.0
            elif pdf_flavor == "acroform":
                # AcroForm text widget: rect IS the input box. Outline the
                # whole box so the model can see the widget's true extent and
                # match nearby captions unambiguously. Tag on the LEFT at
                # vertical centre.
                rect = pymupdf.Rect(x0, y0, x1, y1)
                page.draw_rect(rect, color=(1, 0, 0), width=1.5, overlay=True)
                anchor_y = (y0 + y1) / 2.0
            else:
                # Flat text field: bottom of the placement box ≈ underline
                # (offset by _UNDERLINE_DOWN, typically ~1pt). Draw a thin
                # red marker AT the underline only, so the red rectangle
                # doesn't cover the caption above it.
                underline_y = y1
                marker = pymupdf.Rect(
                    x0, max(0.0, underline_y - 2.5), x1, min(page_h, underline_y + 0.5)
                )
                page.draw_rect(marker, color=(1, 0, 0), width=1.5, overlay=True)
                anchor_y = underline_y

            # Tag sits just LEFT of the field's left edge, centred vertically
            # on the marker/checkbox. Falls to the RIGHT of the field if
            # there's no room on the left (field flush against page edge).
            tag_y_center = min(max(tag_h / 2.0, anchor_y), page_h - tag_h / 2.0)
            tag_y0 = tag_y_center - tag_h / 2.0
            tag_y1 = tag_y_center + tag_h / 2.0
            if x0 >= tag_w + 2.0:
                tag_x0 = x0 - tag_w - 1.0
            else:
                tag_x0 = min(page_w - tag_w, x1 + 1.0)
            tag = pymupdf.Rect(tag_x0, tag_y0, tag_x0 + tag_w, tag_y1)
            page.draw_rect(tag, color=(1, 0, 0), fill=(1, 1, 0), width=0.5, overlay=True)
            page.insert_text(
                (tag.x0 + 2.0, tag.y1 - 2.5),
                f"#{idx}",
                fontsize=8,
                color=(0, 0, 0),
                overlay=True,
            )
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")


async def _run_ocr(
    client: httpx.AsyncClient,
    pdf_bytes: bytes,
    *,
    api_key: str,
    ocr_model: str,
) -> dict[int, str]:
    """POST /v1/ocr and return {page_num (1-based): markdown}."""
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    body = {
        "model": ocr_model,
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{b64}",
        },
        "include_image_base64": False,
    }
    resp = await client.post(
        f"{_MISTRAL_BASE_URL}/ocr",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()
    out: dict[int, str] = {}
    for page in data.get("pages", []):
        try:
            idx = int(page["index"]) + 1
        except (KeyError, TypeError, ValueError):
            continue
        md = str(page.get("markdown") or "").strip()
        if md:
            out[idx] = md
    return out


async def _label_one_batch(
    client: httpx.AsyncClient,
    image_png: bytes,
    ocr_text: str,
    fields: list[dict[str, Any]],
    *,
    page_num: int,
    api_key: str,
    chat_model: str,
) -> dict[int, dict[str, str]]:
    b64 = base64.b64encode(image_png).decode("ascii")
    image_part = {
        "type": "image_url",
        "image_url": f"data:image/png;base64,{b64}",
    }
    index_list = ", ".join(f"#{int(f['index'])}" for f in fields)
    prompt = _PROMPT_TEMPLATE.format(
        page_num=page_num,
        index_list=index_list,
        ocr_text=ocr_text or "(no OCR text extracted for this page)",
    )
    body = {
        "model": chat_model,
        "messages": [
            {
                "role": "user",
                "content": [image_part, {"type": "text", "text": prompt}],
            }
        ],
        "temperature": 0.0,
    }
    resp = await client.post(
        f"{_MISTRAL_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)
    out: dict[int, dict[str, str]] = {}
    for item in data.get("labels", []):
        try:
            idx = int(item["index"])
            lab = str(item.get("label", "")).strip()
            sec = str(item.get("section", "")).strip()
            if lab:
                out[idx] = {"label": lab, "section": sec}
        except (KeyError, TypeError, ValueError):
            continue
    return out


# Geometric field detection thresholds (in PDF points, 1pt = 1/72 inch).
# A US-letter page is 612x792 pt.
_MIN_LINE_LEN_PT = 25.0   # ignore very short strokes (bullet dashes, dividers)
_MAX_LINE_THICK_PT = 3.0  # a "blank line" is thin; thicker strokes are borders
_MIN_BOX_SIDE_PT = 6.0    # smallest checkbox we accept
_MAX_CHECKBOX_PT = 20.0   # anything larger is a text-box, not a checkbox
_TEXT_OVERLAP_TOL_PT = 2.0  # text sitting on/near a line = underline decoration


def _drawings_to_fields(page: Any) -> list[dict[str, Any]]:
    """Extract candidate field boxes from a page's vector drawings.

    Pixel-perfect and hallucination-free: we only report what is actually
    drawn. Returns [] for pages with no vector primitives (scanned/raster).
    """
    import pymupdf  # type: ignore[import-untyped]

    page_w = float(page.rect.width)
    page_h = float(page.rect.height)

    # Grab all text spans once so we can reject lines that sit under body
    # text (underline decorations, strikethroughs).
    text_spans: list[Any] = []
    try:
        td = page.get_text("dict")
        for block in td.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bbox = span.get("bbox")
                    if bbox and len(bbox) == 4:
                        text_spans.append(pymupdf.Rect(*bbox))
    except Exception:
        pass

    def _text_covers(rect: Any, threshold: float = 0.4) -> bool:
        """True if body-text spans cover >threshold of `rect`'s width —
        indicating an underline/strikethrough decoration rather than a blank
        field."""
        if rect.width <= 0:
            return False
        covered = 0.0
        y_band = pymupdf.Rect(
            rect.x0, rect.y0 - 12.0, rect.x1, rect.y1 + _TEXT_OVERLAP_TOL_PT
        )
        for span in text_spans:
            if not span.intersects(y_band):
                continue
            x0 = max(rect.x0, span.x0)
            x1 = min(rect.x1, span.x1)
            if x1 > x0:
                covered += x1 - x0
        return (covered / rect.width) >= threshold

    lines: list[tuple[float, float, float, float]] = []  # (x0,y0,x1,y1)
    rects: list[tuple[float, float, float, float, bool]] = []  # +is_filled

    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []

    for d in drawings:
        stroke_w = float(d.get("width") or 0.0)
        d_type = str(d.get("type") or "")
        is_filled = "f" in d_type and d.get("fill") is not None
        for item in d.get("items", []):
            if not item:
                continue
            kind = item[0]
            if kind == "l" and len(item) >= 3:
                p1, p2 = item[1], item[2]
                x0, y0 = float(p1.x), float(p1.y)
                x1, y1 = float(p2.x), float(p2.y)
                # Only horizontal-ish lines are candidate blanks.
                if abs(y1 - y0) <= 1.5 and abs(x1 - x0) >= _MIN_LINE_LEN_PT:
                    if stroke_w <= _MAX_LINE_THICK_PT:
                        lo, hi = (x0, x1) if x0 <= x1 else (x1, x0)
                        lines.append((lo, y0, hi, y0))
            elif kind == "re" and len(item) >= 2:
                r = item[1]
                rects.append((
                    float(r.x0), float(r.y0), float(r.x1), float(r.y1), is_filled,
                ))

    # Reject lines that form table grids (a near-parallel matching line
    # sits directly above OR below within 40pt AND shares most of the x-range).
    def _is_grid_line(x0: float, y: float, x1: float) -> bool:
        for lx0, ly0, lx1, _ly1 in lines:
            if abs(ly0 - y) < 1.0:
                continue
            if abs(ly0 - y) > 40.0:
                continue
            ov = min(x1, lx1) - max(x0, lx0)
            span = x1 - x0
            if span > 0 and (ov / span) > 0.85:
                return True
        return False

    fields: list[dict[str, Any]] = []

    # A blank line sits at the BASELINE of where the user will write. The
    # renderer places the value baseline at (box_bottom - 0.2*h), so we bias
    # the box UPWARD so the rendered baseline lands well above the line
    # (values written by the user then sit visibly on top of the underline,
    # not straddling or below it). With UP=14, DOWN=-1, h=13 the render
    # baseline is ~3.6pt above the underline.
    _UNDERLINE_UP = 14.0
    _UNDERLINE_DOWN = -1.0

    for x0, y0, x1, _y1 in lines:
        rect = pymupdf.Rect(x0, y0 - _UNDERLINE_UP, x1, y0 + _UNDERLINE_DOWN)
        if _text_covers(rect):
            continue  # underline decoration under body text
        if _is_grid_line(x0, y0, x1):
            continue  # cell border
        fields.append({
            "x_pct": rect.x0 / page_w,
            "y_pct": rect.y0 / page_h,
            "w_pct": (rect.x1 - rect.x0) / page_w,
            "h_pct": (rect.y1 - rect.y0) / page_h,
            "field_type": "text",
        })

    for rx0, ry0, rx1, ry1, is_filled in rects:
        w = rx1 - rx0
        h = ry1 - ry0
        if w <= 0 or h <= 0:
            continue
        # Skip full-page frames (outer border, section band).
        if w > page_w * 0.9 and h > page_h * 0.9:
            continue

        # Thin & wide rectangle = an underline drawn as a filled rect
        # (common for Word/InDesign exports where blanks aren't stroke
        # lines but tall-slim filled shapes). Treat as text field.
        if h < 4.0 and w >= _MIN_LINE_LEN_PT:
            line_y = ry0 + h / 2.0
            candidate = pymupdf.Rect(
                rx0, line_y - _UNDERLINE_UP, rx1, line_y + _UNDERLINE_DOWN
            )
            if _text_covers(candidate):
                continue
            if _is_grid_line(rx0, line_y, rx1):
                continue
            fields.append({
                "x_pct": candidate.x0 / page_w,
                "y_pct": candidate.y0 / page_h,
                "w_pct": (candidate.x1 - candidate.x0) / page_w,
                "h_pct": (candidate.y1 - candidate.y0) / page_h,
                "field_type": "text",
            })
            continue
        # Thin & tall = vertical divider (table column separator) — skip.
        if w < 4.0 and h >= _MIN_LINE_LEN_PT:
            continue

        if w < _MIN_BOX_SIDE_PT or h < _MIN_BOX_SIDE_PT:
            continue
        if is_filled:
            continue  # solid fills at box size are backgrounds, not entry areas
        aspect = w / h if h > 0 else 999
        # Small near-square → checkbox; larger → text entry box.
        if w <= _MAX_CHECKBOX_PT and h <= _MAX_CHECKBOX_PT and 0.6 <= aspect <= 1.6:
            ftype = "checkbox"
        else:
            rect = pymupdf.Rect(rx0, ry0, rx1, ry1)
            if _text_covers(rect):
                continue
            ftype = "text"
        fields.append({
            "x_pct": rx0 / page_w,
            "y_pct": ry0 / page_h,
            "w_pct": w / page_w,
            "h_pct": h / page_h,
            "field_type": ftype,
        })

    # De-duplicate near-identical boxes (a drawn rectangle plus a stroke
    # trace of it, common in Word exports).
    dedup: list[dict[str, Any]] = []
    for f in fields:
        dup = False
        for g in dedup:
            if (
                abs(f["x_pct"] - g["x_pct"]) < 0.005
                and abs(f["y_pct"] - g["y_pct"]) < 0.005
                and abs(f["w_pct"] - g["w_pct"]) < 0.01
                and abs(f["h_pct"] - g["h_pct"]) < 0.01
            ):
                dup = True
                break
        if not dup:
            dedup.append(f)
    return dedup


def _detect_cells(page: Any) -> list[dict[str, Any]]:
    """Reconstruct cell fields from horizontal line pairs.

    Some forms (a vector-glyph PAP form is the canonical example) draw every field
    as a table cell using individual horizontal stroke sub-segments — the
    row's top and bottom edges are each split into one H segment per column,
    and vertical dividers are usually absent or truncated to short stubs at
    row boundaries. `_drawings_to_fields` sees no text spans, treats the
    long-form borders as grid lines, and rejects them all — vision then
    hallucinates the layout back with drift.

    Approach: for each horizontal segment, find the CLOSEST horizontal
    segment BELOW it that shares the same x-range (within tolerance).
    That pair defines a cell rectangle. Row height is capped to reject
    matches that jump across intervening rows.

    Returns [] when there aren't enough segments to make a grid — the caller
    then falls back to vision.
    """
    page_w = float(page.rect.width)
    page_h = float(page.rect.height)

    h_segs: list[tuple[float, float, float]] = []  # (x0, x1, y)
    # Non-border drawing items — used to detect cells that already contain
    # printed captions (i.e. the cell is a LABEL, not a blank input). In
    # vector-glyph PDFs each printed character is a cluster of ~10-30 fill
    # bezier items sitting inside the cell rect.
    glyph_bboxes: list[tuple[float, float, float, float]] = []

    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    for d in drawings:
        for item in d.get("items", []):
            if not item:
                continue
            kind = item[0]
            if kind == "l" and len(item) >= 3:
                p1, p2 = item[1], item[2]
                x0, y0 = float(p1.x), float(p1.y)
                x1, y1 = float(p2.x), float(p2.y)
                dx = abs(x1 - x0)
                dy = abs(y1 - y0)
                if dy < 1.5 and dx >= 15.0:
                    lo, hi = (x0, x1) if x0 <= x1 else (x1, x0)
                    h_segs.append((lo, hi, (y0 + y1) / 2.0))
                    continue
                if dx < 1.5 and dy >= 8.0:
                    # long V — skip both as border segment (we're not
                    # tracking verticals) AND as a glyph item.
                    continue
            # Every other primitive is candidate glyph fill / decoration.
            pts: list[tuple[float, float]] = []
            for sub in item[1:]:
                if hasattr(sub, "x") and hasattr(sub, "y"):
                    pts.append((float(sub.x), float(sub.y)))
                elif hasattr(sub, "x0") and hasattr(sub, "y0"):
                    pts.append((float(sub.x0), float(sub.y0)))
                    pts.append((float(sub.x1), float(sub.y1)))
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            glyph_bboxes.append((min(xs), min(ys), max(xs), max(ys)))

    if len(h_segs) < 4:
        return []

    x_tol = 2.5
    max_row_h = 60.0  # taller than any single form row we expect
    min_row_h = 8.0
    min_cell_w = 15.0

    cells: list[tuple[float, float, float, float]] = []
    for x0, x1, y_top in h_segs:
        if x1 - x0 < min_cell_w:
            continue
        best_bot_y = None
        best_dy = None
        for bx0, bx1, y_bot in h_segs:
            dy = y_bot - y_top
            if dy < min_row_h or dy > max_row_h:
                continue
            if abs(bx0 - x0) > x_tol or abs(bx1 - x1) > x_tol:
                continue
            if best_dy is None or dy < best_dy:
                best_dy = dy
                best_bot_y = y_bot
        if best_bot_y is not None:
            cells.append((x0, y_top, x1, best_bot_y))

    if not cells:
        return []

    tol = 2.0
    dedup: list[tuple[float, float, float, float]] = []
    for c in cells:
        dup = False
        for e in dedup:
            if all(abs(c[k] - e[k]) < tol for k in range(4)):
                dup = True
                break
        if not dup:
            dedup.append(c)

    # Keep only minimal cells (no other cell strictly inside) — drops any
    # row-spanning rectangle that wraps a row of sub-cells because both
    # edges happened to appear as full-width lines.
    minimal: list[tuple[float, float, float, float]] = []
    for c in dedup:
        cx0, cy0, cx1, cy1 = c
        contains_other = False
        for e in dedup:
            if e is c:
                continue
            ex0, ey0, ex1, ey1 = e
            if (
                ex0 >= cx0 - tol and ey0 >= cy0 - tol
                and ex1 <= cx1 + tol and ey1 <= cy1 + tol
                and ((ex1 - ex0) < (cx1 - cx0) - tol or (ey1 - ey0) < (cy1 - cy0) - tol)
            ):
                contains_other = True
                break
        if not contains_other:
            minimal.append(c)

    # Reject cells whose interior already contains printed content — those
    # are label captions ("Patient Name", "Date of Birth"), not blank
    # inputs the user is meant to fill. Vector-glyph PDFs render each
    # character as ~10-30 fill primitives, so a threshold of 10 comfortably
    # separates blank cells (0 items) from labelled ones (100+ items) while
    # tolerating stray decorations.
    inset = 2.0
    label_threshold = 10

    def _content_items(cx0: float, cy0: float, cx1: float, cy1: float) -> int:
        n = 0
        for gx0, gy0, gx1, gy1 in glyph_bboxes:
            if (
                gx0 >= cx0 + inset
                and gx1 <= cx1 - inset
                and gy0 >= cy0 + inset
                and gy1 <= cy1 - inset
            ):
                n += 1
                if n >= label_threshold:
                    return n
        return n

    fields: list[dict[str, Any]] = []
    for cx0, cy0, cx1, cy1 in minimal:
        w = cx1 - cx0
        h = cy1 - cy0
        if w <= 0 or h <= 0:
            continue
        if _content_items(cx0, cy0, cx1, cy1) >= label_threshold:
            continue
        aspect = w / h if h > 0 else 999
        if w <= _MAX_CHECKBOX_PT and h <= _MAX_CHECKBOX_PT and 0.6 <= aspect <= 1.6:
            ftype = "checkbox"
        else:
            ftype = "text"
        fields.append({
            "x_pct": cx0 / page_w,
            "y_pct": cy0 / page_h,
            "w_pct": w / page_w,
            "h_pct": h / page_h,
            "field_type": ftype,
        })
    return fields


_DETECT_PROMPT_TEMPLATE = """You are analyzing PAGE {page_num} of a scanned PDF form.
Find ONLY the empty places a person would fill in.

Return each field's bounding box as fractions of the page
(0.0 to 1.0, origin at top-left).

A valid field is EXACTLY ONE OF:
  - A blank horizontal underline drawn under (or right of) a printed caption,
    with NO text sitting on top of the line.
  - An empty rectangular box drawn for text entry, with the interior EMPTY.
  - A small empty square (5-20px on a side) next to option text — a checkbox.
  - A long horizontal line at the bottom of a section labeled
    "Signature", "Date", etc.

HARD REJECTIONS — do NOT return any of these as fields:
  - Underlines beneath words in paragraph body text (decoration, not entry).
  - Horizontal rules used as section dividers (usually span most of the page width).
  - Cell borders of tables that have content already printed in them.
  - The outer page border or header/footer lines.
  - Anything with text visibly written on top of it.

For each field, return top-left x, y and width, height as fractions,
plus type ("text" | "checkbox" | "signature").

Prefer PRECISION over recall — it is better to return 5 correct fields
than 15 with false positives.

Return STRICT JSON, nothing else:
{{"fields": [{{"x": 0.12, "y": 0.34, "width": 0.30, "height": 0.025, "type": "text"}}]}}"""


def _render_page_plain(pdf_bytes: bytes, page_num: int, *, scale: float = _RENDER_SCALE) -> bytes:
    """Render a PDF page to PNG without annotations — used by the vision fallback."""
    import pymupdf  # type: ignore[import-untyped]

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if page_num < 1 or page_num > doc.page_count:
            raise ValueError(f"page {page_num} out of range for {doc.page_count}-page PDF")
        page = doc.load_page(page_num - 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")


async def _detect_one_page_vision(
    client: httpx.AsyncClient,
    image_png: bytes,
    *,
    page_num: int,
    api_key: str,
    chat_model: str,
) -> list[dict[str, Any]]:
    """Vision fallback for pages with no vector geometry (scanned rasters)."""
    b64 = base64.b64encode(image_png).decode("ascii")
    body = {
        "model": chat_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": f"data:image/png;base64,{b64}"},
                    {"type": "text", "text": _DETECT_PROMPT_TEMPLATE.format(page_num=page_num)},
                ],
            }
        ],
        "temperature": 0.0,
    }
    resp = await client.post(
        f"{_MISTRAL_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)
    out: list[dict[str, Any]] = []
    for item in data.get("fields", []):
        try:
            x = float(item["x"])
            y = float(item["y"])
            w = float(item["width"])
            h = float(item["height"])
        except (KeyError, TypeError, ValueError):
            continue
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.005, min(1.0 - x, w))
        h = max(0.005, min(1.0 - y, h))
        ftype = str(item.get("type") or "text").strip().lower()
        if ftype not in {"text", "checkbox", "signature"}:
            ftype = "text"
        out.append({
            "x_pct": x, "y_pct": y, "w_pct": w, "h_pct": h,
            "field_type": ftype,
        })
    return out


async def detect_fields_in_pdf(
    pdf_bytes: bytes,
    *,
    api_key: str,
    chat_model: str = "pixtral-12b-2409",
    timeout: float = 180.0,
) -> list[dict[str, Any]]:
    """Detect fillable field bounding boxes on a flat PDF, page by page.

    Routing per page:
      * If the page has ANY extractable text: run geometric detection
        (pixel-perfect, no hallucination). Fall back to vision only if
        geometry returns zero candidates (typical for scanned rasters that
        happen to have an OCR text overlay but no vector primitives).
      * If the page has NO extractable text: skip geometry and go straight
        to vision. Two real-world cases fall in here — raster-only scans
        AND vector-drawn PDFs where every glyph is emitted as fill curves
        (the vector-glyph PAP form is 3200+ vector shapes drowning any real
        underline or cell edge in noise, and its cell borders are 4-line
        boxes the grid-line rejector correctly filters out). Geometry
        finds ~1 field on either shape; vision finds ~30.
    """
    import pymupdf  # type: ignore[import-untyped]

    merged: list[dict[str, Any]] = []
    vision_pages: list[tuple[int, float, float, Any]] = []  # (num, w, h, text_spans)

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        page_count = doc.page_count
        for page_num in range(1, page_count + 1):
            page = doc.load_page(page_num - 1)
            # Extract text spans once — used both for the text-layer check
            # AND (in the vision path) to filter model-hallucinated fields
            # that overlap body text.
            spans: list[Any] = []
            text_chars = 0
            try:
                td = page.get_text("dict")
                for block in td.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            bbox = span.get("bbox")
                            if bbox and len(bbox) == 4:
                                spans.append(pymupdf.Rect(*bbox))
                            text_chars += len((span.get("text") or "").strip())
            except Exception:
                pass

            if text_chars == 0:
                # Vector-glyph forms with drawn cell borders (vector-glyph PAP) are
                # invisible to the text-layer check but have a perfectly
                # regular H/V line grid. Try to reconstruct closed cells
                # first — pixel-exact and free — and only fall back to
                # vision if there aren't enough edges to make a grid.
                cells = _detect_cells(page)
                if len(cells) >= 5:
                    for f in cells:
                        merged.append({"page": page_num, **f})
                else:
                    vision_pages.append((
                        page_num, float(page.rect.width), float(page.rect.height), spans,
                    ))
                continue

            geo = _drawings_to_fields(page)
            if geo:
                for f in geo:
                    merged.append({"page": page_num, **f})
            else:
                vision_pages.append((
                    page_num, float(page.rect.width), float(page.rect.height), spans,
                ))

    if vision_pages:
        async with httpx.AsyncClient(timeout=timeout) as client:
            sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

            async def run(
                page_num: int, page_w: float, page_h: float, spans: list[Any]
            ) -> list[dict[str, Any]]:
                async with sem:
                    image_png = _render_page_plain(pdf_bytes, page_num)
                    try:
                        fields = await _detect_one_page_vision(
                            client, image_png,
                            page_num=page_num,
                            api_key=api_key, chat_model=chat_model,
                        )
                    except Exception:
                        # A single bad model response (garbled JSON, transient
                        # 5xx, etc.) shouldn't fail the whole detect call —
                        # skip the page and let the user draw those manually.
                        return []
                    # Filter out vision hallucinations that overlap body text.
                    # Vision models like to invent fields inside paragraphs on
                    # prose-heavy pages; a real fillable region has clear space.
                    kept: list[dict[str, Any]] = []
                    for f in fields:
                        rect_x0 = f["x_pct"] * page_w
                        rect_y0 = f["y_pct"] * page_h
                        rect_x1 = rect_x0 + f["w_pct"] * page_w
                        rect_y1 = rect_y0 + f["h_pct"] * page_h
                        rect = pymupdf.Rect(rect_x0, rect_y0, rect_x1, rect_y1)
                        covered = 0.0
                        w_span = rect.width if rect.width > 0 else 1.0
                        for span in spans:
                            if not span.intersects(rect):
                                continue
                            ox = max(0.0, min(rect.x1, span.x1) - max(rect.x0, span.x0))
                            covered += ox
                        if (covered / w_span) < 0.25:
                            kept.append(f)
                    fields = kept
                    return [{"page": page_num, **f} for f in fields]

            results = await asyncio.gather(*(run(*vp) for vp in vision_pages))
            for r in results:
                merged.extend(r)

    return merged


async def label_fields_in_pdf(
    pdf_bytes: bytes,
    fields: list[dict[str, Any]],
    *,
    api_key: str,
    ocr_model: str = "mistral-ocr-latest",
    chat_model: str = "pixtral-12b-2409",
    timeout: float = 180.0,
    pdf_flavor: str | None = None,
) -> dict[int, dict[str, str]]:
    """OCR the whole PDF, then match numbered widgets to captions per page.

    Each `fields` item must include: `index`, `page`, `x_pct`, `y_pct`,
    `w_pct`, `h_pct`, and optionally `field_type` / `name`. Batches are one
    image per page (splitting further only if a single page has more than
    `_BATCH_SIZE` fields). Batches run concurrently.

    `pdf_flavor` picks the text-widget marker geometry — "acroform" outlines
    the whole widget rect, anything else uses the flat-detection underline
    bar. See `_annotate_and_render_page` for the rationale.

    Returns a mapping from field `index` to `{"label": ..., "section": ...}`.
    Indices the model omits (or returns empty for) are absent from the dict.
    """
    if not fields:
        return {}

    by_page: dict[int, list[dict[str, Any]]] = {}
    for f in fields:
        by_page.setdefault(int(f.get("page") or 1), []).append(f)

    async with httpx.AsyncClient(timeout=timeout) as client:
        ocr_by_page = await _run_ocr(
            client, pdf_bytes, api_key=api_key, ocr_model=ocr_model,
        )

        batches: list[tuple[int, bytes, str, list[dict[str, Any]]]] = []
        for page_num in sorted(by_page):
            page_fields = by_page[page_num]
            page_text = ocr_by_page.get(page_num, "")
            for i in range(0, len(page_fields), _BATCH_SIZE):
                slice_ = page_fields[i : i + _BATCH_SIZE]
                image_png = _annotate_and_render_page(
                    pdf_bytes, page_num, slice_, pdf_flavor=pdf_flavor
                )
                batches.append((page_num, image_png, page_text, slice_))

        sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

        async def run(
            page_num: int,
            image_png: bytes,
            ocr_text: str,
            batch: list[dict[str, Any]],
        ) -> dict[int, dict[str, str]]:
            async with sem:
                return await _label_one_batch(
                    client, image_png, ocr_text, batch,
                    page_num=page_num,
                    api_key=api_key, chat_model=chat_model,
                )

        results = await asyncio.gather(
            *(run(pn, img, txt, b) for pn, img, txt, b in batches)
        )

    merged: dict[int, dict[str, str]] = {}
    for r in results:
        merged.update(r)
    return merged
