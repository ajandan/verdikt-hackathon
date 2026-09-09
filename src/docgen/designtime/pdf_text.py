"""Shared PDF text-extraction primitive.

Four consumers used to reimplement per-page text extraction on top of
pymupdf, each with its own ad-hoc dict shape:

  * `inbox/test_verdict.py::_extract_pdf_text` — visual-order plain text
    for the AI QA verdict scorer.
  * `designtime/pdf_field_detector.py::_drawings_to_fields` — per-span
    rects used to reject underline decorations under body text.
  * `designtime/pdf_field_detector.py::_geometric_labels_for_widgets` —
    per-span rects + font sizes for caption/widget matching.
  * `designtime/pdf_field_detector.py::_ocr_lines_for_page` — per-line
    text with `y=<y0> x=<x0..x1>` anchors for the Nemotron labeler
    prompt.

They now all sit on top of `extract_words(page)` (pdfplumber-backed)
which returns `Word` records with a `Rect` from `designtime.geometry`.
Callers that need visual reading order use `group_into_visual_rows`.

Coordinate model matches the rest of `designtime.geometry`: PDF user
space, origin top-left, +x right, +y down.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docgen.designtime.geometry import Rect


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    rect: Rect
    font_size: float
    font_name: str


@dataclass(frozen=True, slots=True)
class Span:
    """A row-local run of consecutive words with consistent font size.

    Approximates the "span" primitive pymupdf's `get_text("dict")` used to
    emit — the chunk boundary you get when a text run stops or the font
    changes. Caption matching in `pdf_field_detector` relies on span-level
    text ("First Name") rather than per-word candidates ("First", "Name")
    to pick the right label for a widget.
    """
    text: str
    rect: Rect
    font_size: float


def extract_words(page: Any) -> list[Word]:
    """Extract words from a pdfplumber `Page`.

    Order matches pdfplumber's default (roughly content-stream order).
    Consumers that need visual reading order should feed the result into
    `group_into_visual_rows`.
    """
    raw = page.extract_words(extra_attrs=["fontname", "size"]) or []
    return [
        Word(
            text=str(w["text"]),
            rect=Rect(
                float(w["x0"]),
                float(w["top"]),
                float(w["x1"]),
                float(w["bottom"]),
            ),
            font_size=float(w.get("size") or 0.0),
            font_name=str(w.get("fontname") or ""),
        )
        for w in raw
    ]


def group_into_visual_rows(
    words: list[Word], bucket_pt: float = 8.0
) -> list[list[Word]]:
    """Bucket words into visual rows and sort each row left-to-right.

    An 8pt bucket is wider than the largest body font we render (12pt),
    so a single visual row is never split across two buckets. Returned
    list is top-to-bottom.
    """
    rows: dict[int, list[Word]] = {}
    for w in words:
        bucket = round(w.rect.y0 / bucket_pt)
        rows.setdefault(bucket, []).append(w)
    return [sorted(rows[b], key=lambda w: w.rect.x0) for b in sorted(rows)]


def group_into_spans(
    row: list[Word], max_gap_pt: float = 8.0, size_tol: float = 0.5
) -> list[Span]:
    """Cluster a visual row into spans of tight-gap, same-size word runs.

    Two consecutive words join into the same span when the x-gap between
    them is ≤ `max_gap_pt` AND their font sizes agree within `size_tol`.
    Wider gaps signal column boundaries (heading rows across a form) and
    size changes signal a caption→heading transition.
    """
    spans: list[Span] = []
    current: list[Word] = []
    for w in row:
        if not current:
            current.append(w)
            continue
        last = current[-1]
        gap = w.rect.x0 - last.rect.x1
        if gap <= max_gap_pt and abs(w.font_size - last.font_size) <= size_tol:
            current.append(w)
        else:
            spans.append(_finalize_span(current))
            current = [w]
    if current:
        spans.append(_finalize_span(current))
    return spans


def _finalize_span(words: list[Word]) -> Span:
    x0 = min(w.rect.x0 for w in words)
    x1 = max(w.rect.x1 for w in words)
    y0 = min(w.rect.y0 for w in words)
    y1 = max(w.rect.y1 for w in words)
    size = max((w.font_size for w in words), default=0.0)
    text = " ".join(w.text for w in words)
    return Span(text=text, rect=Rect(x0, y0, x1, y1), font_size=size)


def extract_spans(page: Any) -> list[Span]:
    """One-shot: extract words, bucket into rows, split each row into spans.

    Returned in top-to-bottom / left-to-right order.
    """
    words = extract_words(page)
    if not words:
        return []
    spans: list[Span] = []
    for row in group_into_visual_rows(words):
        spans.extend(group_into_spans(row))
    return spans
