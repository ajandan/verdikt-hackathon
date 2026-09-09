"""Coordinate primitives for PDF field detection.

Independent of the PDF backend so `pdf_field_detector.py` can stop importing
`pymupdf.Rect` directly (ADR-0001 Option 2 — replacing pymupdf with
pypdfium2 + pdfplumber to restore an MIT posture).

Rectangles use PDF-user-space coordinates: origin top-left, +x right, +y down.
`x0 <= x1` and `y0 <= y1` are expected but not enforced — mirrors pymupdf.Rect,
which also tolerates inverted corners and lets callers normalize when needed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def intersects(self, other: "Rect") -> bool:
        return (
            self.x0 < other.x1
            and self.x1 > other.x0
            and self.y0 < other.y1
            and self.y1 > other.y0
        )
