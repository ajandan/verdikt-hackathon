"""Text-only PDF field detector + labeler (geometry + Nemotron reasoning).

Detection is 100% geometric — vector drawing primitives (line strokes and
rectangles) are parsed directly from the PDF via pdfplumber. No image is
ever rendered and no vision model is called. `_drawings_to_fields` handles
underline/box widgets, `_detect_cells` handles vector-glyph cell-grid
forms.

Labeling is text-only:
  1. For AcroForm PDFs, `_geometric_labels_for_widgets` first matches each
     widget rect to its printed caption using page text-span geometry —
     zero LLM calls, pixel-exact, works on every field that has a caption
     within reach.
  2. Any widget geometry can't resolve is sent to NVIDIA Nemotron (via
     Nebius Token Factory, OpenAI-compatible /chat/completions dialect).
     The prompt contains per-line OCR text extracted with pdfplumber plus
     the field's coordinates — no image. Nemotron picks the caption that
     sits next to the coordinates. Geometric hits win on conflict.

Returns `{index: {"label": ..., "section": ...}}`. Section is the field's
group heading on the page ("Patient Information Section", etc.), which the
UI slugs into `section_label` payload keys to disambiguate captions that
repeat under different headings.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from typing import Any

import httpx

from docgen.designtime.geometry import Rect
from docgen.designtime.pdf_text import (
    extract_spans,
    extract_words,
    group_into_visual_rows,
)

log = logging.getLogger(__name__)

_BATCH_SIZE = 20
_BATCH_CONCURRENCY = 4
# Max fields per vision-labeling API call. Dense pages (36+ boxes) trigger a
# reading-order bias in the VLM: it labels boxes in top-to-bottom order
# instead of respecting the numbered-marker identity, so mid-page checkboxes
# end up with top-of-page captions. Chunking each page into small groups
# (sorted by reading order) keeps the numbered order aligned with the VLM's
# natural read and drops per-page mis-attribution to near zero.
# 8 was chosen from empirical data: pages with <=13 fields hit 88-92%
# correct; pages with 36+ dropped to 50-56%. Sub-10 is the safe ceiling.
_VISION_CHUNK_SIZE = 8

# Vertical padding above/below a chunk's bounding box when cropping the
# annotated PNG. Chunking alone doesn't kill the reading-order bias because
# the VLM still sees the whole page and latches onto the topmost caption.
# Cropping to just the chunk's region + padding forces the VLM to only see
# the captions immediately near its boxes. Above-padding is larger because
# form captions usually sit ABOVE their inputs. Sides / below get a smaller
# margin — enough to catch a section header to the left or a legend below.
_VISION_CROP_PAD_ABOVE_PX = 90
_VISION_CROP_PAD_OTHER_PX = 30
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
        except (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.TimeoutException) as e:  # noqa: E501
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


def _iter_horizontal_segments(
    page: Any, min_dx: float = 15.0
) -> list[tuple[float, float, float, float]]:
    """Return `(x0, x1, y_center, linewidth)` for every horizontal segment.

    pdfplumber wraps multi-segment stroked paths (rectangle borders, table
    grids, cell outlines drawn as `m l l l l ...`) as a single `curve`
    object — `page.lines` alone misses those. This helper walks each
    curve's `path` list and emits every line-to segment whose endpoints
    share a y-value. Standalone `page.lines` are included too so callers
    get one uniform stream.

    `min_dx` filters out short stubs (bullet dashes, decorative ticks).
    """
    segs: list[tuple[float, float, float, float]] = []
    for ln in (page.lines or []):
        y0 = float(ln["top"])
        y1 = float(ln["bottom"])
        if abs(y1 - y0) > 1.5:
            continue
        x0 = float(ln["x0"])
        x1 = float(ln["x1"])
        if abs(x1 - x0) < min_dx:
            continue
        segs.append((
            min(x0, x1), max(x0, x1), (y0 + y1) / 2.0,
            float(ln.get("linewidth") or 0.0),
        ))
    for c in (page.curves or []):
        lw = float(c.get("linewidth") or 0.0)
        cursor: tuple[float, float] | None = None
        for op in (c.get("path") or []):
            if not op:
                continue
            kind = op[0]
            if kind == "m" and len(op) >= 2:
                cursor = tuple(op[1])
            elif kind == "l" and len(op) >= 2 and cursor is not None:
                nxt = tuple(op[1])
                if abs(cursor[1] - nxt[1]) <= 1.0 and abs(nxt[0] - cursor[0]) >= min_dx:
                    segs.append((
                        min(cursor[0], nxt[0]),
                        max(cursor[0], nxt[0]),
                        (cursor[1] + nxt[1]) / 2.0,
                        lw,
                    ))
                cursor = nxt
            elif kind == "c" and len(op) >= 4:
                cursor = tuple(op[3])
            elif kind in ("v", "y") and len(op) >= 3:
                cursor = tuple(op[2])
            elif kind == "h":
                # close-path back to the last move-to; approximated as no-op
                # (we're only after H strokes and any close-path H edge is
                # already emitted as an "l" segment on well-formed rects).
                pass
    return segs


def _curve_widgets(page: Any) -> list[tuple[float, float, float, float, str]]:
    """Return `(x0, y0, x1, y1, ftype)` for closed square-ish stroked curves.

    Radio buttons are almost always drawn as a circle stroked with 4 bezier
    segments (`m c c c c h`). Checkboxes drawn as rounded rects show up as
    the same shape at slightly different aspect. pdfplumber emits both as
    entries in `page.curves`.

    Filter rules — keep the false-positive rate low on vector-glyph forms
    that draw every printed character as a fill curve:
      * stroked but NOT filled — filled = glyph body, stroked-only = widget
      * bounding box in the checkbox size range (`_MIN_BOX_SIDE_PT` to
        `_MAX_CHECKBOX_PT`), roughly square (0.6-1.6 aspect)
      * at least 2 bezier `c` ops — glyphs like the top-loop of an `f` are
        one `c` op; a closed circle needs at least 2 to trace the arc

    Classification: `n_bez >= 3` → radio (circle drawn as ≥3 beziers);
    otherwise checkbox (rounded rect / stubby curve).
    """
    widgets: list[tuple[float, float, float, float, str]] = []
    for c in (page.curves or []):
        if c.get("fill") or not c.get("stroke"):
            continue
        x0 = float(c["x0"])
        y0 = float(c["top"])
        x1 = float(c["x1"])
        y1 = float(c["bottom"])
        w = x1 - x0
        h = y1 - y0
        if w < _MIN_BOX_SIDE_PT or h < _MIN_BOX_SIDE_PT:
            continue
        if w > _MAX_CHECKBOX_PT or h > _MAX_CHECKBOX_PT:
            continue
        aspect = w / h if h > 0 else 999
        if not (0.6 <= aspect <= 1.6):
            continue
        ops = c.get("path") or []
        n_bez = sum(1 for op in ops if op and op[0] == "c")
        if n_bez < 2:
            continue
        ftype = "radio" if n_bez >= 3 else "checkbox"
        widgets.append((x0, y0, x1, y1, ftype))
    return widgets


def _drawings_to_fields(page: Any) -> list[dict[str, Any]]:
    """Extract candidate field boxes from a page's vector drawings.

    Pixel-perfect and hallucination-free: we only report what is actually
    drawn. Returns [] for pages with no vector primitives (scanned/raster).

    Source primitives come from pdfplumber's `page.lines`, `page.rects`, and
    `page.curves` — the last covers radio-circle glyphs (`m c c c c h`) and
    other rounded widget outlines. Small stroked rects still show up as
    checkboxes via `page.rects`.
    """
    page_w = float(page.width)
    page_h = float(page.height)

    # Grab all text spans once so we can reject lines that sit under body
    # text (underline decorations, strikethroughs).
    text_spans: list[Rect] = [s.rect for s in extract_spans(page)]

    def _text_covers(rect: Rect, threshold: float = 0.4) -> bool:
        """True if body-text spans cover >threshold of `rect`'s width —
        indicating an underline/strikethrough decoration rather than a blank
        field."""
        if rect.width <= 0:
            return False
        covered = 0.0
        y_band = Rect(
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

    for x0, x1, y_c, stroke_w in _iter_horizontal_segments(page, min_dx=_MIN_LINE_LEN_PT):
        if stroke_w <= _MAX_LINE_THICK_PT:
            lines.append((x0, y_c, x1, y_c))

    for r in (page.rects or []):
        rects.append((
            float(r["x0"]),
            float(r["top"]),
            float(r["x1"]),
            float(r["bottom"]),
            bool(r.get("fill")),
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
        rect = Rect(x0, y0 - _UNDERLINE_UP, x1, y0 + _UNDERLINE_DOWN)
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
            candidate = Rect(
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
        # Section-band backgrounds: shaded strips that run across most of the
        # page width and are shallow. Real input widgets never span >70% of
        # the page. Only skip fills — an outlined rect that shape is more
        # likely a wide text-entry frame.
        if is_filled and w > page_w * 0.7 and h < 30.0:
            continue
        aspect = w / h if h > 0 else 999
        # Small near-square → checkbox; larger → text entry box.
        if w <= _MAX_CHECKBOX_PT and h <= _MAX_CHECKBOX_PT and 0.6 <= aspect <= 1.6:
            ftype = "checkbox"
        else:
            rect = Rect(rx0, ry0, rx1, ry1)
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

    for cx0, cy0, cx1, cy1, ftype in _curve_widgets(page):
        fields.append({
            "x_pct": cx0 / page_w,
            "y_pct": cy0 / page_h,
            "w_pct": (cx1 - cx0) / page_w,
            "h_pct": (cy1 - cy0) / page_h,
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

    Cells whose interior overlaps printed characters (`page.chars`) are
    labels, not blanks, and are rejected — vector-glyph forms typically
    have printed captions sitting inside the label cells and blank
    interiors on the input cells.

    Returns [] when there aren't enough segments to make a grid — the caller
    then contributes zero fields for that page.
    """
    page_w = float(page.width)
    page_h = float(page.height)

    h_segs: list[tuple[float, float, float]] = []  # (x0, x1, y)
    for x0, x1, y_c, _lw in _iter_horizontal_segments(page, min_dx=15.0):
        h_segs.append((x0, x1, y_c))

    # Character bboxes (one per rendered glyph) are used to reject cells
    # that already contain printed captions. pdfplumber's `page.chars` gives
    # us a per-glyph rect directly, replacing the old drawings-based glyph
    # heuristic that counted fill sub-primitives.
    glyph_bboxes: list[tuple[float, float, float, float]] = []
    for ch in (page.chars or []):
        glyph_bboxes.append((
            float(ch["x0"]),
            float(ch["top"]),
            float(ch["x1"]),
            float(ch["bottom"]),
        ))

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
    # inputs the user is meant to fill. pdfplumber's `page.chars` gives one
    # entry per rendered glyph, so a 3-char threshold cleanly separates
    # blank cells (0 chars) from labelled ones (10+ chars) while tolerating
    # stray decoration marks.
    inset = 2.0
    label_threshold = 3

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

    Pure geometry — no LLM, no image rendering. For each page we always
    run `_drawings_to_fields` (underlines, boxed inputs, checkboxes drawn
    as small rects, radios drawn as closed bezier circles). On pages with
    no extractable text (vector-glyph forms whose every printed character
    is a fill curve, a vector-glyph PAP form being the canonical example) we
    additionally run `_detect_cells` to reconstruct writable text cells
    from horizontal-line pairs, then merge with dedupe so a widget picked
    up by both passes doesn't count twice. No vision fallback.

    Kept `async` for caller compatibility — every caller already awaits
    this, and a future concurrent-per-page pass would slot in without a
    signature change.
    """
    import pdfplumber  # type: ignore[import-untyped]

    merged: list[dict[str, Any]] = []

    def _overlaps_existing(cand: dict[str, Any], existing: list[dict[str, Any]]) -> bool:
        for g in existing:
            if (
                abs(cand["x_pct"] - g["x_pct"]) < 0.01
                and abs(cand["y_pct"] - g["y_pct"]) < 0.01
                and abs(cand["w_pct"] - g["w_pct"]) < 0.02
                and abs(cand["h_pct"] - g["h_pct"]) < 0.02
            ):
                return True
        return False

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text_chars = sum(
                len((ch.get("text") or "").strip()) for ch in (page.chars or [])
            )

            page_fields = _drawings_to_fields(page)
            if text_chars == 0:
                # Vector-glyph forms hide their captions inside filled curves.
                # Two consequences:
                #   1. `_drawings_to_fields`' "horizontal line → text field"
                #      rule can't reject URL/link underlines and footer strokes
                #      because `_text_covers` has no text spans to consult.
                #      Every stray line becomes a fake text field. Same story
                #      for wide filled rects (header banners). Keep only the
                #      widgets — checkboxes and radios — where the geometric
                #      signal is strong on its own.
                #   2. Real writable text cells come from `_detect_cells`,
                #      which reconstructs closed cells from H-line pairs and
                #      doesn't fire on standalone underlines.
                page_fields = [
                    f for f in page_fields
                    if f.get("field_type") in ("checkbox", "radio")
                ]
                for cell in _detect_cells(page):
                    if not _overlaps_existing(cell, page_fields):
                        page_fields.append(cell)

            for f in page_fields:
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
    import pdfplumber  # type: ignore[import-untyped]

    if not fields:
        return {}

    by_page: dict[int, list[dict[str, Any]]] = {}
    for f in fields:
        by_page.setdefault(int(f.get("page") or 1), []).append(f)

    results: dict[int, dict[str, str]] = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page_fields in by_page.items():
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            page = pdf.pages[page_num - 1]
            pw = float(page.width)
            ph = float(page.height)

            spans: list[dict[str, Any]] = []
            for sp in extract_spans(page):
                spans.append({
                    "rect": sp.rect,
                    "text": sp.text,
                    "size": sp.font_size,
                    "flags": 0,
                })
            if not spans:
                continue

            widget_rects: list[tuple[int, Any, str]] = []
            for f in page_fields:
                try:
                    idx = int(f["index"])
                    wr = Rect(
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

Every listed index must appear exactly once."""  # noqa: E501


def pdf_has_extractable_text(pdf_bytes: bytes) -> bool:
    """True if any page yields extractable text words via pdfplumber.

    Vector-glyph forms (every letter drawn as a fill curve) return False —
    they have zero text objects, so caption-based labeling can't run. The
    UI uses this to explain a 0-labeled result instead of silently failing.
    """
    import pdfplumber  # type: ignore[import-untyped]

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                if extract_words(page):
                    return True
    except Exception:
        return True
    return False


def _ocr_lines_for_page(page: Any) -> str:
    """Dump one text-line-per-row of the page as `y=<y0> x=<x0..x1>: <text>`,
    top-to-bottom. Matches the format used by `scripts/qc_nemotron_labeling.py`.

    Rows come from the shared `extract_words` + `group_into_visual_rows`
    primitive so every consumer sees the same reading order.
    """
    words = extract_words(page)
    if not words:
        return ""
    rows: list[tuple[float, float, float, str]] = []
    for row in group_into_visual_rows(words):
        if not row:
            continue
        x0 = min(w.rect.x0 for w in row)
        x1 = max(w.rect.x1 for w in row)
        y0 = min(w.rect.y0 for w in row)
        rows.append((y0, x0, x1, " ".join(w.text for w in row)))
    rows.sort(key=lambda r: (round(r[0], 1), r[1]))
    return "\n".join(
        f"y={y0:.1f} x={x0:.1f}..{x1:.1f}: {txt}"
        for y0, x0, x1, txt in rows
    )


_VISION_LABEL_PROMPT = """This page from a form has {n_boxes} numbered red boxes drawn on it.
Each widget is the red-outlined rectangle. Its NUMBER is inside a red
circle sitting on the widget's TOP-LEFT corner (half on, half off the
widget). The caption is printed TEXT ON THE PAGE — usually ABOVE or
just LEFT of the widget on the same row.

Return STRICT JSON in this shape (no prose, no code fences):

{{"labels": [
  {{"box": 1, "section": "Patient Information", "label": "First Name"}},
  {{"box": 2, "section": "Patient Information", "label": "Date of Birth"}},
  ...
]}}

Rules:
- Every box from 1 to {n_boxes} must appear exactly once.
- `label`: the specific field caption printed near that box ("First Name",
  "Date of Birth", "Diagnosis Code", "Signature", "Yes", "No", etc.).
- `section`: the SECTION HEADING that box sits under ("Patient Information",
  "Insurance Details", "Physician Certification", ...). If you can't
  identify a section, use "".
- If a box is a checkbox or radio, `label` should describe the OPTION
  (e.g. "Male", "Female", "Yes — has other coverage", "Medicare Part D").
- ALWAYS make your best guess from visible text near the box. Use
  `label` = "unknown" ONLY when the page area around the box is truly
  blank (no printed text within ~2cm)."""


def _annotate_page_with_boxes(
    pdf_bytes: bytes,
    page_num: int,
    fields: list[dict[str, Any]],
    page_w: float,
    page_h: float,
    scale: float = 3.125,
) -> tuple[bytes, list[int]]:
    """Rasterize a page and draw a numbered red box for each field.

    Returns (annotated_png_bytes, box_number_to_field_index_list).
    Box numbers are 1-indexed and correspond to the position in the returned
    field-index list.

    Tag placement: numbered marker is drawn as a filled circle CENTERED ON
    the widget's top-left corner — half on the widget, half outside. The
    earlier below-right placement made the VLM read captions from the row
    BELOW instead of the row above (labels shifted by one row on dense
    forms). Sitting the marker ON the widget's corner makes ownership
    unambiguous without covering the caption that sits above.

    Raster: 3.125x scale ≈ 225 DPI. Higher than the previous 2.083x (150
    DPI) so 6-8pt printed captions stay legible after JPEG compression on
    the VLM side.
    """
    from PIL import ImageDraw, ImageFont

    png = _rasterize_page_png(pdf_bytes, page_num, scale=scale)
    if not png:
        return b"", []

    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    except OSError:
        font = ImageFont.load_default()

    box_to_field_idx: list[int] = []
    px_w, px_h = img.size
    x_scale = px_w / page_w
    y_scale = px_h / page_h

    for box_num, f in enumerate(fields, start=1):
        try:
            fx0 = float(f.get("x_pct") or 0) * page_w * x_scale
            fy0 = float(f.get("y_pct") or 0) * page_h * y_scale
            fx1 = fx0 + float(f.get("w_pct") or 0) * page_w * x_scale
            fy1 = fy0 + float(f.get("h_pct") or 0) * page_h * y_scale
            idx = int(f["index"])
        except (KeyError, TypeError, ValueError):
            continue
        # Red rectangle around the widget so the VLM can see the field's
        # exact extent, not just the anchor point.
        draw.rectangle([(fx0, fy0), (fx1, fy1)], outline=(220, 30, 30), width=3)
        # Numbered circular marker centered on the widget's TOP-LEFT corner.
        # Half-on / half-off makes ownership unambiguous — the marker is
        # visually attached to this widget, not the one above or below.
        # Radius sized to fit the number plus a couple pixels of padding.
        label = str(box_num)
        try:
            bbox = draw.textbbox((0, 0), label, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except AttributeError:
            tw, th = (11 * len(label), 14)
        radius = max(tw, th) / 2 + 5
        cx = fx0
        cy = fy0
        draw.ellipse(
            [(cx - radius, cy - radius), (cx + radius, cy + radius)],
            fill=(220, 30, 30),
            outline=(255, 255, 255),
            width=2,
        )
        # Center the number in the circle (glyph baseline sits below the
        # bbox top, so nudge up by a couple pixels).
        text_x = cx - tw / 2
        text_y = cy - th / 2 - 2
        draw.text((text_x, text_y), label, fill=(255, 255, 255), font=font)
        box_to_field_idx.append(idx)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue(), box_to_field_idx


def _crop_annotated_to_chunk(
    annotated_png: bytes,
    chunk: list[dict[str, Any]],
    page_w: float,
    page_h: float,
    scale: float = 3.125,
) -> bytes:
    """Crop the full-page annotated PNG down to just the region covering
    `chunk`'s fields, plus padding.

    Why: the VLM's reading-order bias reaches across the entire visible
    image. A chunk of 8 mid-page boxes on a full-page image causes the VLM
    to read the topmost caption on the page and attribute it to box #1,
    even when box #1 sits at y=0.50. Cropping to the chunk's bounding
    region (+ padding) limits what the VLM can even see — the only captions
    in view are the ones that actually belong to the boxes it's labeling.

    Padding is asymmetric: more space above than below/sides because form
    captions almost always sit above their input widgets (or to their left
    on the same row, which the horizontal padding covers).
    """
    from PIL import Image

    img = Image.open(io.BytesIO(annotated_png)).convert("RGB")
    px_w, px_h = img.size
    x_scale = px_w / page_w
    y_scale = px_h / page_h

    # Bounding box of the chunk's fields in image pixels.
    xs: list[float] = []
    ys: list[float] = []
    for f in chunk:
        try:
            fx0 = float(f.get("x_pct") or 0) * page_w * x_scale
            fy0 = float(f.get("y_pct") or 0) * page_h * y_scale
            fx1 = fx0 + float(f.get("w_pct") or 0) * page_w * x_scale
            fy1 = fy0 + float(f.get("h_pct") or 0) * page_h * y_scale
        except (TypeError, ValueError):
            continue
        xs.extend([fx0, fx1])
        ys.extend([fy0, fy1])

    if not xs or not ys:
        return annotated_png  # nothing to crop against — send full page

    x0 = max(0, int(min(xs)) - _VISION_CROP_PAD_OTHER_PX)
    y0 = max(0, int(min(ys)) - _VISION_CROP_PAD_ABOVE_PX)
    x1 = min(px_w, int(max(xs)) + _VISION_CROP_PAD_OTHER_PX)
    y1 = min(px_h, int(max(ys)) + _VISION_CROP_PAD_OTHER_PX)

    cropped = img.crop((x0, y0, x1, y1))
    out = io.BytesIO()
    cropped.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def _vision_label_page(
    client: httpx.AsyncClient,
    annotated_png: bytes,
    box_to_field_idx: list[int],
    *,
    api_key: str,
    vision_model: str,
    base_url: str,
    extra_body: dict[str, Any] | None = None,
) -> dict[int, dict[str, str]]:
    """Send an annotated page (numbered red boxes) to the VLM and parse the
    returned per-box labels.

    Skips the OCR-then-match indirection: the VLM sees each field's exact
    location AND the caption printed next to it, and returns a label per
    box. Returns `{field_index: {"label": ..., "section": ...}}`.
    """
    import base64

    if not box_to_field_idx or not annotated_png:
        return {}

    b64 = base64.b64encode(annotated_png).decode("ascii")
    prompt = _VISION_LABEL_PROMPT.format(n_boxes=len(box_to_field_idx))
    body: dict[str, Any] = {
        "model": vision_model,
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
        # Reasoning-mode VLMs like MiniCPM-V can burn the whole output budget
        # on a <think>...</think> preamble and truncate before emitting JSON.
        # 8k gives room for the preamble; enable_thinking=false silences it
        # entirely when the served model honors the flag.
        "max_tokens": 8192,
        "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if extra_body:
        body.update(extra_body)
    resp = await _post_with_retry(
        client,
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body=body,
        op="vision_label",
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"] or ""
    data = _extract_json(content)
    items = data.get("labels", [])
    out: dict[int, dict[str, str]] = {}
    dropped_unknown = 0
    dropped_bad = 0
    for item in items:
        try:
            box_num = int(item["box"])
            label = str(item.get("label", "")).strip()
            section = str(item.get("section", "")).strip()
        except (KeyError, TypeError, ValueError):
            dropped_bad += 1
            continue
        if box_num < 1 or box_num > len(box_to_field_idx):
            dropped_bad += 1
            continue
        if not label or label.lower() == "unknown":
            dropped_unknown += 1
            continue
        field_idx = box_to_field_idx[box_num - 1]
        out[field_idx] = {"label": label, "section": section}
    log.info(
        "vision_label_parsed model=%s boxes=%s items=%s kept=%s unknown=%s bad=%s",
        vision_model, len(box_to_field_idx), len(items),
        len(out), dropped_unknown, dropped_bad,
    )
    return out


def _rasterize_page_png(pdf_bytes: bytes, page_num: int, scale: float = 2.083) -> bytes:
    """One PDF page → PNG bytes via pypdfium2 (Apache-2.0)."""
    import pypdfium2  # type: ignore[import-untyped]

    doc = pypdfium2.PdfDocument(pdf_bytes)
    try:
        if page_num < 1 or page_num > len(doc):
            return b""
        page = doc[page_num - 1]
        pil = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


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
    vision_model: str = "",
    labeler_mode: str = "auto",
) -> dict[int, dict[str, str]]:
    """Label fields using geometry first, Nemotron for anything left over.

    Each `fields` item must include: `index`, `page`, `x_pct`, `y_pct`,
    `w_pct`, `h_pct`, and optionally `field_type` / `name`.

    Behavior:
      * `pdf_flavor == "acroform"`: run `_geometric_labels_for_widgets`
        first — for AcroForm PDFs it resolves most widgets with pixel
        accuracy off the real /Rect entries. Widgets it can't resolve fall
        through to the model-based labelers.
      * For each page with remaining unresolved fields the routing depends
        on `labeler_mode`:
          - "auto"   (default): text-only Nemotron for pages with
                     extractable text; vision fallback only where none.
          - "vision": force vision-labeling on every page even when text
                     is extractable. Beats text-only on dense multi-column
                     forms where Nemotron drops labels as empty. Slower.
          - "text":  force text-only labeling; never call the VLM even
                     when the page has no extractable text.
      * Geometric labels overlay model labels — on conflict, geometry
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

    import pdfplumber  # type: ignore[import-untyped]

    by_page: dict[int, list[dict[str, Any]]] = {}
    for f in remaining:
        by_page.setdefault(int(f.get("page") or 1), []).append(f)

    # Prepare per-page OCR dumps + page sizes once so we don't reopen the PDF
    # for every batch.
    page_ocr: dict[int, str] = {}
    page_size: dict[int, tuple[float, float]] = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num in by_page:
            if page_num < 1 or page_num > len(pdf.pages):
                page_ocr[page_num] = ""
                page_size[page_num] = (612.0, 792.0)
                continue
            page = pdf.pages[page_num - 1]
            page_ocr[page_num] = _ocr_lines_for_page(page)
            page_size[page_num] = (float(page.width), float(page.height))

    # Vision-labeling pages: rasterize with each detected widget outlined
    # in a numbered red box and send the annotated image to the VLM. The
    # VLM sees each field's position AND the caption printed next to it,
    # so we skip the OCR-then-spatial-match indirection entirely — it
    # returns `{box: label}` directly.
    #   - "auto"   (default): only pages with no extractable text (vector-
    #              glyph forms) go through vision.
    #   - "vision": every page goes through vision, even text-bearing ones.
    #              Wins on dense multi-column forms where Nemotron's
    #              coordinate reasoning drops labels as empty.
    #   - "text":  no page goes through vision.
    if labeler_mode == "vision":
        vision_pages = list(by_page.keys())
    elif labeler_mode == "text":
        vision_pages = []
    else:
        vision_pages = [pn for pn in by_page if not page_ocr.get(pn, "").strip()]
    visual_labels: dict[int, dict[str, str]] = {}

    async with httpx.AsyncClient(timeout=timeout) as client:
        sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

        if vision_model and vision_pages:
            # Split each page's fields into small reading-order chunks
            # (`_VISION_CHUNK_SIZE`). Each chunk becomes its own annotated
            # image + its own VLM call. Sorting by (y_pct, x_pct) first
            # aligns the drawn numbering with the VLM's natural reading
            # order — the mid-page-checkbox-labeled-as-top-of-page-caption
            # failure mode disappears because there are only 8 boxes per
            # image and they all sit in the same horizontal band.
            vision_chunks: list[tuple[int, list[dict[str, Any]]]] = []
            for pn in vision_pages:
                page_fields = sorted(
                    by_page[pn],
                    key=lambda f: (float(f.get("y_pct") or 0), float(f.get("x_pct") or 0)),
                )
                for i in range(0, len(page_fields), _VISION_CHUNK_SIZE):
                    vision_chunks.append((pn, page_fields[i : i + _VISION_CHUNK_SIZE]))

            async def _visual_label_chunk(
                pn: int, chunk: list[dict[str, Any]]
            ) -> dict[int, dict[str, str]]:
                async with sem:
                    pw, ph = page_size.get(pn, (612.0, 792.0))
                    annotated, box_map = _annotate_page_with_boxes(
                        pdf_bytes, pn, chunk, pw, ph,
                    )
                    if not annotated or not box_map:
                        return {}
                    # Crop the full-page annotated PNG to just this chunk's
                    # bounding region + padding. Without this the VLM still
                    # sees every caption on the page and its reading-order
                    # bias attributes top-of-page captions to whichever box
                    # happens to be #1 in the chunk (even when box #1 sits
                    # mid-page). Cropping tightly around the chunk means the
                    # only captions in the VLM's field of view are the ones
                    # that actually belong to these boxes.
                    cropped = _crop_annotated_to_chunk(annotated, chunk, pw, ph)
                    try:
                        return await _vision_label_page(
                            client, cropped, box_map,
                            api_key=api_key,
                            vision_model=vision_model,
                            base_url=base_url,
                        )
                    except httpx.HTTPStatusError as e:
                        log.warning(
                            "vision_label_http_error page=%s chunk=%d model=%s status=%s body=%r",
                            pn, len(chunk), vision_model, e.response.status_code,
                            e.response.text[:400],
                        )
                        return {}
                    except Exception as e:
                        log.warning(
                            "vision_label_failed page=%s chunk=%d model=%s err=%s: %s",
                            pn, len(chunk), vision_model, type(e).__name__, e,
                        )
                        return {}

            per_chunk_results = await asyncio.gather(
                *(_visual_label_chunk(pn, chunk) for pn, chunk in vision_chunks)
            )
            for r in per_chunk_results:
                visual_labels.update(r)

        # Text-model batches cover pages that weren't already sent to vision.
        # In "auto" mode that's every page with extractable text; in "vision"
        # mode every page was vision-labeled so this list is empty; in "text"
        # mode every page routes through the text labeler regardless of
        # whether it has extractable text (Nemotron will get an empty OCR
        # dump and drop most labels, but the escape hatch is intentional).
        vision_page_set = set(vision_pages)
        batches: list[tuple[int, list[dict[str, Any]]]] = []
        for page_num in sorted(by_page):
            if page_num in vision_page_set:
                continue
            if labeler_mode != "text" and not page_ocr.get(page_num, "").strip():
                continue
            page_fields = by_page[page_num]
            for i in range(0, len(page_fields), _BATCH_SIZE):
                batches.append((page_num, page_fields[i : i + _BATCH_SIZE]))

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
    # Visual-labeling hits (from vector-glyph pages) go in next. They were
    # produced by the VLM off the rasterized page, so they're the only signal
    # available for those fields — trump nothing on text pages.
    merged.update(visual_labels)
    # Geometric hits are authoritative — they already matched a real caption
    # bbox to a real widget rect. Overlay them last so any Nemotron guess for
    # the same index is superseded.
    merged.update(geometric)
    return merged
