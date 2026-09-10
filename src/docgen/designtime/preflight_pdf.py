"""Preflight validation for PDF templates — the PDF analogue of core.preflight.

Two flavors emerge from the same source PDF:
  - "acroform": the PDF has widget fields. We harvest each widget's page+rect
                into an initial pdf_field_placements list, so the user can
                render immediately without opening the flat-editor. The output
                is still a flat overlay — the widgets themselves are discarded
                at render time (see core.render_pipeline_pdf).
  - "flat":     no widgets. placements starts empty; the user must place fields
                in the PR2 editor before the template can be rendered.

Reject reasons: not-a-PDF, encrypted, oversize, and text-less flat PDFs.
The last one catches "Microsoft Print to PDF" output and raster-only scans —
both produce a PDF with no `page.chars`, which leaves the AI labeler with
no captions to match, sections undetectable, and realignment with no text
anchor. Users should re-export via "Save As PDF" (which preserves the text
layer) instead of "Print to PDF".
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Literal

import pypdf

from docgen.core.preflight import Finding, Severity

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100


@dataclass
class PdfPreflightReport:
    outcome: Literal["accept", "accept-with-warnings", "reject"]
    flavor: Literal["acroform", "flat"] | None = None
    page_count: int = 0
    findings: list[Finding] = field(default_factory=list)
    # For acroform: pre-populated placements harvested from widgets.
    # For flat: empty — the user fills these in the editor.
    initial_placements: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "flavor": self.flavor,
            "page_count": self.page_count,
            "findings": [
                {"code": f.code, "severity": f.severity.value, "message": f.message, "hint": f.hint}
                for f in self.findings
            ],
            "initial_placements": self.initial_placements,
        }


def _reject(code: str, message: str, hint: str | None = None) -> PdfPreflightReport:
    return PdfPreflightReport(
        outcome="reject",
        findings=[Finding(code=code, severity=Severity.ERROR, message=message, hint=hint)],
    )


def _classify_ft(ft: Any) -> tuple[str | None, str]:
    """PDF /FT → (field_type_for_schema, placement_type_for_render).

    Kept independent of pypdf's field wrapper so we can call it from either
    the AcroForm-tree walker or the page-annotation fallback below.
    """
    if ft == "/Tx":
        return "text", "text"
    if ft == "/Btn":
        # Radios / pushbuttons share /Btn but are rare here; treat as checkbox
        # so we don't left-pad checkbox glyphs off their box at render time.
        return "checkbox", "checkbox"
    if ft == "/Ch":
        return "choice", "text"  # rendered as text until we ship a picker
    return None, "text"


def _make_placement(
    *,
    name: str,
    label: Any,
    ft: Any,
    rect: Any,
    page_num: int,
    page_w: float,
    page_h: float,
) -> dict[str, Any] | None:
    """Compute a placement dict from a widget /Rect on a known page.

    Returns None if the rect is malformed. Coordinates are page-relative
    fractions with top-left origin (editor convention); the render pipeline
    flips y at draw time.
    """
    if not rect or len(rect) != 4 or page_w <= 0 or page_h <= 0:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in rect)
    except (TypeError, ValueError):
        return None
    left = min(x1, x2)
    right = max(x1, x2)
    bottom = min(y1, y2)
    top = max(y1, y2)
    field_type, placement_type = _classify_ft(ft)
    font_size = max(8.0, min(18.0, (top - bottom) * 0.7))
    return {
        "name": str(name),
        "label": str(label) if label is not None else None,
        "field_type": field_type,
        "type": placement_type,
        "page": page_num,
        "x_pct": round(left / page_w, 5),
        "y_pct": round((page_h - top) / page_h, 5),
        "w_pct": round((right - left) / page_w, 5),
        "h_pct": round((top - bottom) / page_h, 5),
        "font_size": round(font_size, 1),
        "anchor": None,  # populated for flat flavor in PR2/PR3
    }


def _harvest_widget_placements(reader: pypdf.PdfReader) -> list[dict[str, Any]]:
    """Extract one placement per widget across all pages.

    Two harvest paths, tried in order:

      1. Walk /AcroForm/Fields — real-world AcroForms (Adobe LiveCycle,
         Acrobat Pro, most large-org forms) split the field from the widget:
         the *field* object carries /T (the name) and /FT (the type), and
         its /Kids array points at one-or-more widget annotations that hold
         /Rect. Names are inherited from parents ("GroupA.claim_id"). Simply
         iterating page annotations misses these because /T lives on the
         parent, not on the widget annot.

      2. Fall back to iterating each page's /Annots for any widgets we
         haven't already emitted — handles the merged field/widget shape
         reportlab produces (single object with /T + /Rect + /FT).

    Coordinates are page-relative fractions (0..1) with top-left origin.
    Pages are 1-based to match the editor UI.
    """
    placements: list[dict[str, Any]] = []
    seen_widget_ids: set[int] = set()

    # Build page-object → 1-based-index lookup keyed by indirect-reference id
    # so a widget's /P back-reference resolves to the page number in O(1).
    page_by_ref: dict[int, tuple[int, Any]] = {}
    for page_idx, page in enumerate(reader.pages):
        try:
            ref = page.indirect_reference
            if ref is not None:
                page_by_ref[ref.idnum] = (page_idx + 1, page)
        except AttributeError:
            continue

    def _widget_page(widget: Any) -> tuple[int, Any] | None:
        """Locate the page that owns this widget, using /P then /Annots scan."""
        p_ref = widget.get("/P")
        if p_ref is not None:
            try:
                hit = page_by_ref.get(p_ref.idnum)
                if hit is not None:
                    return hit
            except AttributeError:
                pass
        # Fallback: scan /Annots on every page for the widget object id.
        try:
            widget_id = widget.indirect_reference.idnum
        except AttributeError:
            return None
        for page_idx, page in enumerate(reader.pages):
            annots = page.get("/Annots")
            if not annots:
                continue
            for a_ref in annots:
                try:
                    if a_ref.idnum == widget_id:
                        return (page_idx + 1, page)
                except AttributeError:
                    continue
        return None

    def _walk_field(field_ref: Any, parent_name: str, inherited_ft: Any, inherited_tu: Any) -> None:
        try:
            field = field_ref.get_object() if hasattr(field_ref, "get_object") else field_ref
        except Exception:
            return
        t = field.get("/T")
        # PDF spec: fully-qualified name is parent.T when both present.
        if t is not None:
            full_name = f"{parent_name}.{t}" if parent_name else str(t)
        else:
            full_name = parent_name
        # /FT is inherited from parent when a child field doesn't set its own.
        ft = field.get("/FT") or inherited_ft
        # /TU (tooltip) also lives on the parent when widgets are pure annots.
        tu = field.get("/TU") or inherited_tu
        kids = field.get("/Kids")
        subtype = field.get("/Subtype")

        # Merged field/widget: same dict has both /T and /Rect + /Subtype=/Widget.
        # Emit immediately and don't descend into kids (there usually aren't any).
        if subtype == "/Widget" and field.get("/Rect") is not None and full_name:
            _emit(field, full_name, ft, tu)
            return

        if kids:
            # Peek at the first kid to decide: are these sub-fields (recurse)
            # or widget annotations (emit one placement per widget)?
            for kid_ref in kids:
                try:
                    kid = kid_ref.get_object()
                except Exception:
                    continue
                if kid.get("/Subtype") == "/Widget" and kid.get("/T") is None:
                    # Kid is a pure widget under this field → emit.
                    if full_name:
                        _emit(kid, full_name, ft, tu)
                else:
                    # Kid is a child field (has its own /T, or is a sub-tree).
                    _walk_field(kid_ref, full_name, ft, tu)
            return

        # Terminal field with a rect but no /Subtype=/Widget — rare, but the
        # spec allows it. Treat as a widget on the /P page.
        if field.get("/Rect") is not None and full_name:
            _emit(field, full_name, ft, tu)

    def _emit(widget: Any, name: str, ft: Any, parent_label: Any = None) -> None:
        try:
            widget_id = widget.indirect_reference.idnum
        except AttributeError:
            widget_id = None
        if widget_id is not None and widget_id in seen_widget_ids:
            return
        loc = _widget_page(widget)
        if loc is None:
            return
        page_num, page = loc
        try:
            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)
        except Exception:
            return
        # /TU (tooltip) is the human label the AI-map step slugs into a
        # payload key. In the tree-shaped AcroForm the tooltip lives on the
        # parent field, not on the widget kid — that's parent_label. Fall
        # back to the widget's own /TU / /T for the merged shape.
        label = parent_label or widget.get("/TU") or widget.get("/T")
        p = _make_placement(
            name=name, label=label, ft=ft, rect=widget.get("/Rect"),
            page_num=page_num, page_w=page_w, page_h=page_h,
        )
        if p is None:
            return
        placements.append(p)
        if widget_id is not None:
            seen_widget_ids.add(widget_id)

    # Path 1: walk /AcroForm/Fields.
    try:
        root = reader.trailer["/Root"]
        acro_form = root.get("/AcroForm") if root is not None else None
    except Exception:
        acro_form = None
    if acro_form is not None:
        try:
            acro_form = acro_form.get_object() if hasattr(acro_form, "get_object") else acro_form
        except Exception:
            acro_form = None
    if acro_form is not None:
        fields = acro_form.get("/Fields")
        if fields:
            for f_ref in fields:
                _walk_field(f_ref, parent_name="", inherited_ft=None, inherited_tu=None)

    # Path 2: page-annotation fallback for merged widgets not caught above
    # (e.g. rogue widgets not linked from the /AcroForm/Fields root).
    for page_idx, page in enumerate(reader.pages):
        try:
            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)
        except Exception:
            continue
        annots = page.get("/Annots")
        if annots is None:
            continue
        for annot_ref in annots:
            try:
                annot = annot_ref.get_object()
            except Exception:
                continue
            try:
                annot_id = annot_ref.idnum
            except AttributeError:
                annot_id = None
            if annot_id is not None and annot_id in seen_widget_ids:
                continue
            if annot.get("/Subtype") != "/Widget":
                continue
            name = annot.get("/T")
            if not name:
                continue
            ft = annot.get("/FT")
            label = annot.get("/TU") or annot.get("/T")
            p = _make_placement(
                name=str(name), label=label, ft=ft, rect=annot.get("/Rect"),
                page_num=page_idx + 1, page_w=page_w, page_h=page_h,
            )
            if p is None:
                continue
            placements.append(p)
            if annot_id is not None:
                seen_widget_ids.add(annot_id)

    return placements


def preflight_pdf(pdf_bytes: bytes) -> PdfPreflightReport:
    if not pdf_bytes:
        return _reject("pdf.empty", "PDF is empty.")

    if len(pdf_bytes) > MAX_PDF_BYTES:
        return _reject(
            "pdf.oversize",
            f"PDF exceeds {MAX_PDF_BYTES // (1024 * 1024)} MB limit "
            f"({len(pdf_bytes) // (1024 * 1024)} MB).",
            "Compress the PDF (Acrobat → Reduce File Size) or split into smaller documents.",
        )

    if not pdf_bytes.startswith(b"%PDF-"):
        return _reject(
            "pdf.invalid",
            "File is not a PDF (missing %PDF- header).",
            "Save the source as PDF and re-upload.",
        )

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        return _reject("pdf.parse_error", f"Could not parse PDF: {e}")

    if reader.is_encrypted:
        return _reject(
            "pdf.encrypted",
            "PDF is password-protected — the render pipeline cannot open it.",
            "Remove the password in Acrobat (File → Properties → Security) and re-upload.",
        )

    page_count = len(reader.pages)
    if page_count == 0:
        return _reject("pdf.no_pages", "PDF has zero pages.")
    if page_count > MAX_PAGES:
        return _reject(
            "pdf.too_many_pages",
            f"PDF has {page_count} pages (max {MAX_PAGES}).",
            "Split the document into smaller templates.",
        )

    placements = _harvest_widget_placements(reader)

    findings: list[Finding] = []
    if placements:
        flavor: Literal["acroform", "flat"] = "acroform"
    else:
        flavor = "flat"
        # No widgets AND no extractable text = vector-glyph PDF, produced by
        # "Microsoft Print to PDF" (rasterizes every glyph into vector curves)
        # or a raster-only scan. Reject at upload: the labeler has no captions
        # to match (pdf_field_detector no-ops when text_chars == 0), sections
        # can't be derived from headings, and realign_pdf has no text anchors
        # on re-upload.
        has_text = False
        for page in reader.pages:
            try:
                if (page.extract_text() or "").strip():
                    has_text = True
                    break
            except Exception:
                continue
        if not has_text:
            return _reject_no_text_layer(reader)

    outcome: Literal["accept", "accept-with-warnings", "reject"] = (
        "accept-with-warnings" if findings else "accept"
    )
    return PdfPreflightReport(
        outcome=outcome,
        flavor=flavor,
        page_count=page_count,
        findings=findings,
        initial_placements=placements,
    )


def _reject_no_text_layer(reader: pypdf.PdfReader) -> PdfPreflightReport:
    """Build the `pdf.no_text_layer` reject with a producer-aware hint.

    When the PDF's `/Producer` metadata reveals the Windows Print-to-PDF
    driver we can point the operator at the specific fix ("use Save As PDF
    instead"); otherwise fall back to a generic scan-or-print hint.
    """
    producer = ""
    try:
        producer = str((reader.metadata or {}).get("/Producer") or "")
    except Exception:
        pass
    if "Print To PDF" in producer or "Print to PDF" in producer:
        message = (
            f"PDF was generated by '{producer}' — the print driver "
            "rasterized text into vector curves, so there is no text "
            "layer for the AI labeler or realignment to anchor against."
        )
        hint = (
            "Re-export the source document via File → Save As PDF "
            "(Word) or File → Export as PDF, instead of File → Print → "
            "Microsoft Print to PDF."
        )
    else:
        message = (
            "PDF has no widgets and no extractable text — the template "
            "cannot be labeled or realigned without a text layer."
        )
        hint = (
            "Save the source as a text-flat PDF (Word: File → Save As "
            "PDF). Scanned pages must be OCR'd first."
        )
    return _reject("pdf.no_text_layer", message, hint)
