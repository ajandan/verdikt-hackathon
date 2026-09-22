"""Snap field-highlight bboxes to actual text positions in the intake PDF.

The template's `pdf_field_placements` come from the design-time labeled
template PDF. When a reviewer opens the intake PDF (typed, e-fax, or
scan with OCR text layer), those bboxes drift because the intake is a
different document.

For any intake PDF that has a text layer, we can do better: search each
page for the actual extracted values (e.g., `patient_info_first_name =
"Betty"`) and snap the highlight bbox to wherever those words appear.

Uses pdfplumber (already a dep — see `designtime/geometry.py` ADR note
about replacing pymupdf for licensing). No image processing, no OCR,
no OpenCV — just pdfplumber's built-in `extract_words()` on each page.

Returns per-slot {page, x_pct, y_pct, w_pct, h_pct} with the same
shape as `slot_placements`, so the caller can merge with template
placements (snapped wins over template when found). Pure image scans
without a text layer return an empty dict; the caller falls back to
template placements.
"""
from __future__ import annotations

import io
from typing import Any

import pdfplumber

# Minimum characters in an extracted value to bother snapping. Very
# short values ("A", "1", "y") match too many spots on the page to be
# useful — the first match wins and it's almost never the right one.
_MIN_VALUE_LEN = 3

# Word-match padding (as fraction of page dim) so the highlight rect
# isn't glued to the exact glyph edges — matches the visual style of
# the design-time placements which include some breathing room.
_PAD_X = 0.004
_PAD_Y = 0.003


def snap_placements_to_pdf_text(
    pdf_bytes: bytes,
    extraction: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Search the intake PDF for each extracted value's text.

    Args:
        pdf_bytes: Raw intake PDF bytes (typed or OCR-preserved scan).
        extraction: {slot_name: value} — the extractor's output.

    Returns:
        {slot_name: {page: int, x: float, y: float, w: float, h: float}}
        for slots whose value was found in the PDF text. Coords are
        page-fraction (0..1), top-left origin. Missing slots fall back
        to the template's `pdf_field_placements` on the caller side.
    """
    if not pdf_bytes or not extraction:
        return {}

    # Build a slot → normalized-search-term map. Skip values that are
    # too short, non-string (booleans, numbers stringified separately),
    # or empty. Booleans/checkboxes don't have text to search for.
    search_terms: dict[str, str] = {}
    for slot, value in extraction.items():
        if value is None or isinstance(value, bool):
            continue
        s = str(value).strip()
        if len(s) < _MIN_VALUE_LEN:
            continue
        # Take the first 40 chars — long-address values match better on
        # a prefix than trying to match the whole thing across word wraps.
        search_terms[slot] = s[:40]

    if not search_terms:
        return {}

    snapped: dict[str, dict[str, Any]] = {}

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_idx, page in enumerate(pdf.pages, start=1):
                page_w = float(page.width) or 1.0
                page_h = float(page.height) or 1.0
                try:
                    words = page.extract_words(
                        x_tolerance=2, y_tolerance=2, keep_blank_chars=False,
                    )
                except Exception:
                    continue
                if not words:
                    continue

                # For each slot's search term, find the best matching
                # word (or contiguous run of words) on this page.
                for slot, term in list(search_terms.items()):
                    if slot in snapped:
                        continue  # already found on an earlier page
                    hit = _find_term_on_page(term, words, page_w, page_h)
                    if hit is not None:
                        hit["page"] = page_idx
                        snapped[slot] = hit
    except Exception:
        # pdfplumber can raise on malformed PDFs — fall back cleanly.
        return snapped

    return snapped


def _find_term_on_page(
    term: str,
    words: list[dict[str, Any]],
    page_w: float,
    page_h: float,
) -> dict[str, Any] | None:
    """Find `term` among page words, return page-fraction bbox or None.

    Strategy: scan every starting-index and look for the LONGEST
    contiguous run of words whose concatenated text (normalized) is
    either equal to `term` or a substring / superstring within 90%
    overlap. Track all candidates; pick the one with the highest
    match score. Length-based rejection eliminates the "one tiny word
    matches everything" failure mode of a loose `in` check.
    """
    tnorm = _normalize(term)
    if not tnorm or len(tnorm) < _MIN_VALUE_LEN:
        return None

    n = len(words)
    best_score = 0.0
    best_range: tuple[int, int] | None = None

    for i in range(n):
        acc = ""
        for j in range(i, min(i + 8, n)):
            piece = _normalize(words[j].get("text", ""))
            if not piece:
                continue
            acc = (acc + " " + piece).strip() if acc else piece
            # Score = overlap ratio of the shorter into the longer.
            score = _match_score(acc, tnorm)
            if score > best_score:
                best_score = score
                best_range = (i, j)
            # If we've already exceeded term length by a lot, stop
            # growing — further words can only dilute the match.
            if len(acc) > len(tnorm) * 1.5:
                break

    # Require a strong match — 0.9 = at least 90% of the shorter string
    # is a prefix of the longer. Anything below is too speculative.
    if best_range is None or best_score < 0.9:
        return None

    i, j = best_range
    xs = [words[k]["x0"] for k in range(i, j + 1)]
    ys = [words[k]["top"] for k in range(i, j + 1)]
    xe = [words[k]["x1"] for k in range(i, j + 1)]
    ye = [words[k]["bottom"] for k in range(i, j + 1)]
    return _bbox_pct(min(xs), min(ys), max(xe), max(ye), page_w, page_h)


def _match_score(a: str, b: str) -> float:
    """Overlap ratio of the shorter string onto the longer.

    1.0 = one is a prefix of the other with equal length (perfect match).
    0.9+ = shorter is a full prefix of longer, missing ≤10% of longer.
    Below 0.5 = weak, likely a coincidence.
    """
    if not a or not b:
        return 0.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    # Both directions: prefix, suffix, or contained substring.
    if long_.startswith(short) or long_.endswith(short) or short in long_:
        return len(short) / len(long_)
    return 0.0


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace + strip punctuation edges."""
    return " ".join(s.lower().split()).strip(".,;:()[]{}\"'/-")


def _bbox_pct(
    x0: float, y0: float, x1: float, y1: float,
    page_w: float, page_h: float,
) -> dict[str, Any]:
    """PDF pixel bbox → page-fraction bbox with a small pad."""
    x_pct = max(0.0, (x0 / page_w) - _PAD_X)
    y_pct = max(0.0, (y0 / page_h) - _PAD_Y)
    w_pct = min(1.0 - x_pct, (x1 - x0) / page_w + 2 * _PAD_X)
    h_pct = min(1.0 - y_pct, (y1 - y0) / page_h + 2 * _PAD_Y)
    return {"x": x_pct, "y": y_pct, "w": w_pct, "h": h_pct}
