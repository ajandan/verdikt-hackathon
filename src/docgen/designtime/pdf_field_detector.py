"""Text-only PDF field detector + labeler (geometry + Nemotron reasoning).

Detection is 100% geometric — vector drawing primitives are parsed directly
from the PDF via pymupdf (`_drawings_to_fields` for underline/box widgets,
`_detect_cells` for cell-grid forms). No image is ever rendered and no
vision model is called.

Labeling is text-only:
  1. For AcroForm PDFs, `_geometric_labels_for_widgets` first matches each
     widget rect to its printed caption using page text-span geometry —
     zero LLM calls, pixel-exact, works on every field that has a caption
     within reach.
  2. Any widget geometry can't resolve is sent to NVIDIA Nemotron (via
     Nebius Token Factory, OpenAI-compatible /chat/completions dialect).
     The prompt contains per-line OCR text extracted with pymupdf plus
     the field's coordinates — no image. Nemotron picks the caption that
     sits next to the coordinates. Geometric hits win on conflict.

Returns `{index: {"label": ..., "section": ...}}`. Section is the field's
group heading on the page ("Patient Information Section", etc.), which the
UI slugs into `section_label` payload keys to disambiguate captions that
repeat under different headings.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

_BATCH_SIZE = 20
_BATCH_CONCURRENCY = 4
# Nebius Token Factory returns 429/503 during transient overload. One failed
# labeling batch cancels the whole `label_fields_in_pdf` gather, which drops
# every AI label for the template and leaves it stuck at raw AcroForm names
# like "Text Field 158". Retry with jitter so a 900ms blip doesn't kill the
# ingest.
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BACKOFF_BASE_S = 1.5


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    op: str,
) -> httpx.Response:
    """POST with retry on 429/5xx and connection errors.

    Backoff = base * 2**attempt + small jitter, honoring Retry-After when the
    server sends one. Raises the last exception if all attempts fail so the
    caller's existing error-handling still applies.
    """
    import random

    last_exc: Exception | None = None
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            resp = await client.post(url, headers=headers, json=json_body)
        except (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt == _RETRY_MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(
                _RETRY_BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 0.5)
            )
            continue
        if resp.status_code < 500 and resp.status_code != 429:
            return resp
        # Transient upstream error — retry.
        if attempt == _RETRY_MAX_ATTEMPTS - 1:
            return resp  # let caller's raise_for_status() surface the final error
        retry_after = resp.headers.get("retry-after")
        try:
            wait_s = float(retry_after) if retry_after else 0.0
        except ValueError:
            wait_s = 0.0
        wait_s = max(
            wait_s,
            _RETRY_BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 0.5),
        )
        await asyncio.sleep(wait_s)
    # Unreachable — the loop always returns or raises.
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{op}: exhausted retries without response")


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first balanced `{...}` block from the model's response.

    A greedy `\\{.*\\}` regex breaks when the model wraps JSON in prose
    ('Here is the answer: {...}. Let me know if...') or emits multiple
    objects — the greedy match spans them all and json.loads chokes at a
    random offset. Bracket-counting is cheap and correct.

    Also handles reasoning-mode preambles: some open-source LLMs emit
    `<think>...</think>` chain-of-thought before the JSON body, and can
    truncate mid-preamble if max_tokens is too tight. Strip closed
    `<think>...</think>` blocks; if a `<think>` opens but doesn't close,
    drop everything up to (and including) the last visible reasoning line
    so we still find a JSON body if one is present after — otherwise the
    extractor's ValueError below propagates.
    """
    stripped = text.strip()
    # Strip closed <think>...</think> blocks anywhere in the response.
    stripped = re.sub(r"<think>[\s\S]*?</think>", "", stripped, flags=re.IGNORECASE)
    # If an unterminated <think> remains, everything after it is reasoning
    # tail with no JSON — trim it so the caller's error message quotes the
    # actual model output rather than the CoT preamble.
    m = re.search(r"<think>", stripped, flags=re.IGNORECASE)
    if m:
        stripped = stripped[: m.start()].strip()
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
    long-form borders as grid lines, and rejects them all.

    Approach: for each horizontal segment, find the CLOSEST horizontal
    segment BELOW it that shares the same x-range (within tolerance).
    That pair defines a cell rectangle. Row height is capped to reject
    matches that jump across intervening rows.

    Returns [] when there aren't enough segments to make a grid — the caller
    then contributes zero fields for that page.
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


async def detect_fields_in_pdf(pdf_bytes: bytes) -> list[dict[str, Any]]:
    """Detect fillable field bounding boxes on a flat PDF, page by page.

    Pure geometry — no LLM, no image rendering. For each page:
      * If the page has ANY extractable text: run `_drawings_to_fields`
        (horizontal-line and rectangle detection over the page's vector
        primitives). This is pixel-perfect and covers underline-style and
        boxed-input widgets.
      * If the page has NO extractable text (vector-glyph forms whose
        every printed character is a fill curve, e.g. a vector-glyph PAP form
        form), try `_detect_cells` to reconstruct closed cells from
        horizontal-line pairs.
      * If neither returns anything, the page contributes zero fields.
        There is no vision fallback.

    Kept `async` for caller compatibility — every caller already awaits
    this, and a future concurrent-per-page pass would slot in without a
    signature change.
    """
    import pymupdf  # type: ignore[import-untyped]

    merged: list[dict[str, Any]] = []

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        page_count = doc.page_count
        for page_num in range(1, page_count + 1):
            page = doc.load_page(page_num - 1)
            text_chars = 0
            try:
                td = page.get_text("dict")
                for block in td.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            text_chars += len((span.get("text") or "").strip())
            except Exception:
                pass

            if text_chars == 0:
                # Vector-glyph forms with drawn cell borders (vector-glyph PAP) are
                # invisible to the text-layer check but have a perfectly
                # regular H/V line grid. Reconstruct closed cells; if
                # there aren't enough edges to make a grid, this page
                # contributes zero fields (no vision fallback).
                cells = _detect_cells(page)
                for f in cells:
                    merged.append({"page": page_num, **f})
                continue

            geo = _drawings_to_fields(page)
            for f in geo:
                merged.append({"page": page_num, **f})

    return merged


def _geometric_labels_for_widgets(
    pdf_bytes: bytes,
    fields: list[dict[str, Any]],
) -> dict[int, dict[str, str]]:
    """Match each widget rect to its printed caption using page text-span
    geometry. Works for AcroForm PDFs where widget rects come from the real
    /Rect entries — no LLM needed.

    Text field: caption is the closest text span sitting ABOVE the widget
    with x-column overlap (columns don't cross — a "First Name" caption
    won't be picked for a "Middle Initial" widget in the next column).
    Falls back to the closest span sitting to the LEFT on the same row.

    Checkbox / radio: caption is the closest span TO THE RIGHT on the same
    row (standard checkbox-option layout).

    Section: the last H1/H2-style heading printed above the widget. Heuristic
    is font-size ≥ 14pt (real form headings are usually 14pt+ bold; 12pt
    caught body-caption bolds and stray bullet numbers).

    Returns `{index: {"label", "section"}}` only for widgets with a confident
    match. Ambiguous / no-match widgets are omitted so the caller can fall
    back to the Nemotron labeler for those.
    """
    import pymupdf  # type: ignore[import-untyped]

    if not fields:
        return {}

    by_page: dict[int, list[dict[str, Any]]] = {}
    for f in fields:
        by_page.setdefault(int(f.get("page") or 1), []).append(f)

    results: dict[int, dict[str, str]] = {}
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_num, page_fields in by_page.items():
            if page_num < 1 or page_num > doc.page_count:
                continue
            page = doc.load_page(page_num - 1)
            pw = float(page.rect.width)
            ph = float(page.rect.height)

            spans: list[dict[str, Any]] = []
            try:
                td = page.get_text("dict")
                for block in td.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            bbox = span.get("bbox")
                            text = (span.get("text") or "").strip()
                            if not text or not bbox or len(bbox) != 4:
                                continue
                            spans.append({
                                "rect": pymupdf.Rect(*bbox),
                                "text": text,
                                "size": float(span.get("size") or 0),
                                "flags": int(span.get("flags") or 0),
                            })
            except Exception:
                continue
            if not spans:
                continue

            widget_rects: list[tuple[int, Any, str]] = []
            for f in page_fields:
                try:
                    idx = int(f["index"])
                    wr = pymupdf.Rect(
                        float(f.get("x_pct") or 0) * pw,
                        float(f.get("y_pct") or 0) * ph,
                        (float(f.get("x_pct") or 0) + float(f.get("w_pct") or 0)) * pw,
                        (float(f.get("y_pct") or 0) + float(f.get("h_pct") or 0)) * ph,
                    )
                    ftype = str(f.get("field_type") or "text").lower()
                except (KeyError, TypeError, ValueError):
                    continue
                widget_rects.append((idx, wr, ftype))

            # Real section headings on typical form pages are ≥ 14pt (usually
            # bold too). 12pt caught bold body captions like "First Name" and
            # 4-6pt bullet numbers (both flagged bold), so the section field
            # ended up as the caption itself or a stray digit. 14pt lands
            # cleanly between headings (22pt / 14pt observed) and captions
            # (9-10pt) on the templates we've tested.
            heading_spans = sorted(
                [s for s in spans if s["size"] >= 14.0],
                key=lambda s: s["rect"].y0,
            )

            for idx, wr, ftype in widget_rects:
                section = ""
                for hs in heading_spans:
                    if hs["rect"].y1 < wr.y0:
                        section = hs["text"]
                    else:
                        break

                label = ""
                if ftype == "checkbox":
                    cands: list[tuple[float, str]] = []
                    for s in spans:
                        sr = s["rect"]
                        if sr.y1 < wr.y0 - 2 or sr.y0 > wr.y1 + 2:
                            continue
                        if sr.x0 < wr.x1 - 2:
                            continue
                        gap = sr.x0 - wr.x1
                        if gap > 100:
                            continue
                        cands.append((gap, s["text"]))
                    cands.sort()
                    if cands:
                        label = cands[0][1]
                else:
                    above: list[tuple[float, str]] = []
                    for s in spans:
                        sr = s["rect"]
                        if sr.y1 > wr.y0 + 3:
                            continue
                        gap = wr.y0 - sr.y1
                        if gap > 40:
                            continue
                        ox = min(sr.x1, wr.x1) - max(sr.x0, wr.x0)
                        if ox <= 0:
                            continue
                        # Reject if another widget owns this span more strongly
                        # (closer vertically AND with larger x-overlap).
                        stolen = False
                        for oi, ow, _oft in widget_rects:
                            if oi == idx:
                                continue
                            if ow.y0 > sr.y1 and (ow.y0 - sr.y1) < gap:
                                oox = min(sr.x1, ow.x1) - max(sr.x0, ow.x0)
                                if oox > ox:
                                    stolen = True
                                    break
                        if stolen:
                            continue
                        above.append((gap, s["text"]))
                    above.sort()
                    if above:
                        label = above[0][1]

                if label:
                    results[idx] = {"label": label, "section": section}
    return results


_NEMOTRON_PROMPT_TEMPLATE = """You are labeling fillable fields on PAGE {page_num} of a PDF form using positional coordinates and OCR text. NO image is provided.

Each field is a blank rectangle where a user would type or check a value. Pick the printed caption that describes THIS specific field.

Rules for `label`:
- USUALLY the printed text immediately to the LEFT of the field on the same row (same y), OR immediately ABOVE the field in the same x-column.
- Prefer a SHORT caption phrase (e.g. "First Name", "Date", "Amount"), not a full sentence. If the field sits after a phrase like "for the amount of US$ ___", the label is the noun the phrase is asking for ("Amount", not the whole sentence).
- For checkboxes: caption is the option text immediately to the RIGHT.
- Use OCR text verbatim for wording.
- Return "" if nothing sensible fits.

Rules for `section`:
- The nearest heading printed above the field (e.g. "Patient Information Section"). Empty string if no heading applies.
- ALWAYS try to fill this if there is any bold/larger/heading-shaped text above the field on the page.

--- OCR text lines for page {page_num} (top-to-bottom; each line is `y=<y0> x=<x0..x1>: <text>`) ---
{ocr_dump}
--- END OCR ---

Fields to label. Each is `#<idx> at (x=<x0..x1>, y=<y0..y1>) [type]`:
{fields_dump}

Return STRICT JSON:
{{"labels": [{{"index": 0, "section": "", "label": "..."}}, ...]}}

Every listed index must appear exactly once."""


def _ocr_lines_for_page(page: Any) -> str:
    """Dump one text-line-per-row of the page as `y=<y0> x=<x0..x1>: <text>`,
    top-to-bottom. Matches the format used by `scripts/qc_nemotron_labeling.py`.
    """
    rows: list[tuple[float, float, float, str]] = []  # (y0, x0, x1, text)
    try:
        td = page.get_text("dict")
    except Exception:
        return ""
    for block in td.get("blocks", []):
        for line in block.get("lines", []):
            parts: list[str] = []
            xs0: list[float] = []
            xs1: list[float] = []
            ys0: list[float] = []
            for span in line.get("spans", []):
                text = (span.get("text") or "").strip()
                if not text:
                    continue
                bbox = span.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                parts.append(text)
                xs0.append(float(bbox[0]))
                xs1.append(float(bbox[2]))
                ys0.append(float(bbox[1]))
            if not parts:
                continue
            rows.append((min(ys0), min(xs0), max(xs1), " ".join(parts)))
    rows.sort(key=lambda r: (round(r[0], 1), r[1]))
    return "\n".join(
        f"y={y0:.1f} x={x0:.1f}..{x1:.1f}: {txt}"
        for y0, x0, x1, txt in rows
    )


def _format_fields_dump(
    fields: list[dict[str, Any]], page_w: float, page_h: float
) -> str:
    """Render `fields` as `#<idx> at (x=<x0..x1>, y=<y0..y1>) [type]` lines."""
    out: list[str] = []
    for f in fields:
        try:
            idx = int(f["index"])
            x0 = float(f.get("x_pct") or 0) * page_w
            y0 = float(f.get("y_pct") or 0) * page_h
            x1 = x0 + float(f.get("w_pct") or 0) * page_w
            y1 = y0 + float(f.get("h_pct") or 0) * page_h
            ftype = str(f.get("field_type") or "text").lower()
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            f"#{idx} at (x={x0:.1f}..{x1:.1f}, y={y0:.1f}..{y1:.1f}) [{ftype}]"
        )
    return "\n".join(out)


async def _label_one_page_text(
    client: httpx.AsyncClient,
    fields: list[dict[str, Any]],
    ocr_dump: str,
    page_w: float,
    page_h: float,
    *,
    page_num: int,
    api_key: str,
    text_model: str,
    base_url: str,
    extra_body: dict[str, Any] | None = None,
) -> dict[int, dict[str, str]]:
    """One Nemotron chat/completions call: label all `fields` on this page."""
    fields_dump = _format_fields_dump(fields, page_w, page_h)
    prompt = _NEMOTRON_PROMPT_TEMPLATE.format(
        page_num=page_num,
        ocr_dump=ocr_dump or "(no OCR text extracted for this page)",
        fields_dump=fields_dump,
    )
    body: dict[str, Any] = {
        "model": text_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    if extra_body:
        body.update(extra_body)
    resp = await _post_with_retry(
        client,
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body=body,
        op="nemotron_label",
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


async def label_fields_in_pdf(
    pdf_bytes: bytes,
    fields: list[dict[str, Any]],
    *,
    api_key: str,
    text_model: str,
    base_url: str,
    timeout: float = 180.0,
    pdf_flavor: str | None = None,
    extra_body: dict[str, Any] | None = None,
) -> dict[int, dict[str, str]]:
    """Label fields using geometry first, Nemotron for anything left over.

    Each `fields` item must include: `index`, `page`, `x_pct`, `y_pct`,
    `w_pct`, `h_pct`, and optionally `field_type` / `name`.

    Behavior:
      * `pdf_flavor == "acroform"`: run `_geometric_labels_for_widgets`
        first — for AcroForm PDFs it resolves most widgets with pixel
        accuracy off the real /Rect entries. Widgets it can't resolve fall
        through to Nemotron.
      * For each page with remaining unresolved fields: extract per-line
        OCR text with pymupdf's `page.get_text("dict")`, format the fields
        as `#<idx> at (x=..y=..) [type]` lines, and send ONE Nemotron chat
        call per page (or per 20-field batch if a page has more). Runs up
        to `_BATCH_CONCURRENCY` batches in parallel.
      * Geometric labels overlay Nemotron labels — on conflict, geometry
        wins because it matched a real caption bbox to a real widget rect.

    `text_model` is the Nemotron model id (e.g.
    `nvidia/nemotron-3-super-120b-a12b`). `base_url` is the Nebius Token
    Factory endpoint root (no trailing slash), e.g.
    `https://api.studio.nebius.com/v1`.

    Returns a mapping from field `index` to `{"label": ..., "section": ...}`.
    Indices with no confident label are absent from the dict.
    """
    if not fields:
        return {}

    # AcroForm PDFs give us widget rects with pixel accuracy and the page has
    # native text spans with bboxes — caption matching is a solved geometric
    # problem, no LLM needed. Only pass fields geometry can't resolve to
    # Nemotron.
    geometric: dict[int, dict[str, str]] = {}
    remaining: list[dict[str, Any]] = fields
    if pdf_flavor == "acroform":
        try:
            geometric = _geometric_labels_for_widgets(pdf_bytes, fields)
        except Exception:
            geometric = {}
        if geometric:
            remaining = [f for f in fields if int(f.get("index", -1)) not in geometric]

    if not remaining:
        return geometric

    import pymupdf  # type: ignore[import-untyped]

    by_page: dict[int, list[dict[str, Any]]] = {}
    for f in remaining:
        by_page.setdefault(int(f.get("page") or 1), []).append(f)

    # Prepare per-page OCR dumps + page sizes once so we don't reopen the PDF
    # for every batch.
    page_ocr: dict[int, str] = {}
    page_size: dict[int, tuple[float, float]] = {}
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_num in by_page:
            if page_num < 1 or page_num > doc.page_count:
                page_ocr[page_num] = ""
                page_size[page_num] = (612.0, 792.0)
                continue
            page = doc.load_page(page_num - 1)
            page_ocr[page_num] = _ocr_lines_for_page(page)
            page_size[page_num] = (float(page.rect.width), float(page.rect.height))

    # One batch = one Nemotron call. A page with >20 fields is split into
    # chunks; each chunk is still labeled with the same per-page OCR dump.
    batches: list[tuple[int, list[dict[str, Any]]]] = []
    for page_num in sorted(by_page):
        page_fields = by_page[page_num]
        for i in range(0, len(page_fields), _BATCH_SIZE):
            batches.append((page_num, page_fields[i : i + _BATCH_SIZE]))

    async with httpx.AsyncClient(timeout=timeout) as client:
        sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

        async def run(
            page_num: int, batch: list[dict[str, Any]]
        ) -> dict[int, dict[str, str]]:
            async with sem:
                pw, ph = page_size.get(page_num, (612.0, 792.0))
                try:
                    return await _label_one_page_text(
                        client, batch, page_ocr.get(page_num, ""), pw, ph,
                        page_num=page_num,
                        api_key=api_key,
                        text_model=text_model,
                        base_url=base_url,
                        extra_body=extra_body,
                    )
                except Exception:
                    # A single-batch failure after retry shouldn't wipe out
                    # every other page's labels. Return empty so the merged
                    # result just lacks labels for that batch — the ingest
                    # still gets partial labeling on all the others.
                    return {}

        results = await asyncio.gather(
            *(run(pn, b) for pn, b in batches)
        )

    merged: dict[int, dict[str, str]] = {}
    for r in results:
        merged.update(r)
    # Geometric hits are authoritative — they already matched a real caption
    # bbox to a real widget rect. Overlay them last so any Nemotron guess for
    # the same index is superseded.
    merged.update(geometric)
    return merged
