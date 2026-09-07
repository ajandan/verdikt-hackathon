"""Render pipeline for kind='pdf' templates.

Unified overlay strategy for both flavors: the source PDF is treated as a
background layer, values are drawn on top with reportlab, and the two are
merged with pypdf. Interactive form widgets on the source (if any) are
stripped from the output — kept only for extraction on re-upload (PR3).

Placement schema (stored in TemplateVersion.pdf_field_placements['placements']):
  {
    "name":       str,   # payload key
    "page":       int,   # 1-based page number
    "x_pct":      float, # top-left x, page-relative fraction (0.0..1.0)
    "y_pct":      float, # top-left y, page-relative fraction
    "w_pct":      float, # width, page-relative fraction
    "h_pct":      float, # height (for baseline math), optional — falls back
                         # to font_size when zero/absent (editor doesn't
                         # collect h explicitly).
    "font_size":  float, # points
    "anchor":     str | None,  # PR2/PR3: nearest-label text for re-align
    "type":       str | None,  # "text" | "checkbox" | "radio"; when absent,
                               # derived from legacy field_type / payload_type.
  }
"""
from __future__ import annotations

import io
import uuid
from typing import Any

import pypdf
from pypdf.generic import ArrayObject
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from docgen.config import Settings
from docgen.store.blob import get_object, put_object


class PdfRenderError(ValueError):
    """Raised when a PDF template cannot be rendered."""


_CHECKBOX_FALSY_STRINGS = frozenset({"", "no", "false", "off", "0", "n", "unchecked", "off ", "/off"})


def _stringify(value: Any, *, render_as_checkbox: bool = False) -> str:
    if value is None:
        return ""
    if render_as_checkbox:
        # Checkboxes render as a single "X" glyph when truthy and nothing
        # when falsy — writing "Yes"/"No" overflows the tiny widget box.
        # Accept bools, common truthy-string forms, and numeric truthy.
        if isinstance(value, bool):
            return "X" if value else ""
        if isinstance(value, str):
            return "" if value.strip().lower() in _CHECKBOX_FALSY_STRINGS else "X"
        return "X" if value else ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _placement_type(p: dict[str, Any]) -> str:
    """Resolve the placement's semantic type with backward compat.

    New placements carry an explicit "type" field. Legacy placements (frozen
    before the field was introduced) are inferred from the older signals:
    an AcroForm-harvested /Btn sets field_type="checkbox", and the editor's
    checkbox tool sets payload_type="boolean".
    """
    explicit = p.get("type")
    if explicit in ("text", "checkbox", "radio"):
        return explicit
    if p.get("field_type") == "checkbox" or p.get("payload_type") == "boolean":
        return "checkbox"
    return "text"


def _draw_checkbox(
    canv: canvas.Canvas,
    x_left: float,
    y_top: float,
    w: float,
    h: float,
    page_h: float,
    font_size: float,
    value: str,
) -> None:
    """Draw the check glyph when truthy, nothing otherwise.

    No outline: on AcroForm PDFs the source already draws its own frame,
    and overlaying ours produced a doubled border in prod. Flat PDFs are
    accepted as a tradeoff — checked cells show a bare X, unchecked cells
    stay blank.
    """
    if not value:
        return
    box_h = h if h > 0 else font_size * 1.3
    box_w = w if w > 0 else box_h
    box_bottom = page_h - y_top - box_h
    canv.setFont("Helvetica-Bold", font_size)
    text_w = canv.stringWidth(value, "Helvetica-Bold", font_size)
    cx = x_left + box_w / 2.0
    baseline_y = box_bottom + (box_h / 2.0) - (font_size * 0.35)
    canv.drawString(cx - text_w / 2.0, baseline_y, value)


def _draw_radio(
    canv: canvas.Canvas,
    x_left: float,
    y_top: float,
    w: float,
    h: float,
    page_h: float,
    font_size: float,
    is_selected: bool,
) -> None:
    """Draw a circle outline plus a smaller filled circle when selected.

    Same rationale as _draw_checkbox: the outline must draw even when unset
    so the reader can see there's a field there. Inner filled dot is ~55% of
    the outer radius — leaves visible ring around the dot so the shape reads
    as a radio button rather than a solid blob.
    """
    box_h = h if h > 0 else font_size * 1.3
    box_w = w if w > 0 else box_h
    box_bottom = page_h - y_top - box_h
    # Radios are drawn on the largest circle that fits the placement box.
    radius = min(box_w, box_h) / 2.0
    cx = x_left + box_w / 2.0
    cy = box_bottom + box_h / 2.0
    canv.setLineWidth(0.75)
    canv.setStrokeColorRGB(0, 0, 0)
    canv.circle(cx, cy, radius, stroke=1, fill=0)
    if is_selected:
        canv.setFillColorRGB(0, 0, 0)
        canv.circle(cx, cy, radius * 0.55, stroke=0, fill=1)


def _is_truthy(value: Any) -> bool:
    """Reuse the checkbox truthiness rules so radio and checkbox agree on
    what "selected" means. `_stringify(..., render_as_checkbox=True)` returns
    a non-empty "X" for truthy values and "" for falsy ones."""
    return bool(_stringify(value, render_as_checkbox=True))


def _draw_text(
    canv: canvas.Canvas,
    x_left: float,
    y_top: float,
    h: float,
    page_h: float,
    font_size: float,
    value: str,
) -> None:
    """Text fields: small left inset, baseline 20% up from the box bottom."""
    baseline_y = page_h - y_top - h + (h * 0.2)
    canv.setFont("Helvetica", font_size)
    canv.drawString(x_left + 4.0, baseline_y, value)


def _draw_overlay_page(
    canv: canvas.Canvas,
    page_w: float,
    page_h: float,
    placements: list[dict[str, Any]],
    data: dict[str, Any],
) -> None:
    """Draw one page's worth of placements onto the given reportlab canvas.

    PDF coordinate origin is bottom-left; placements are stored top-left, so
    we flip y here. Dispatch is by placement type: text values get a plain
    string, checkboxes get a framed box with an optional check glyph.
    """
    canv.setPageSize((page_w, page_h))
    for p in placements:
        ptype = _placement_type(p)
        value = _stringify(
            data.get(p["name"], ""), render_as_checkbox=(ptype == "checkbox")
        )
        x_left = float(p["x_pct"]) * page_w
        y_top = float(p["y_pct"]) * page_h
        widget_font_size = float(p.get("font_size") or 11.0)
        h_pct = float(p.get("h_pct") or 0.0)
        w_pct = float(p.get("w_pct") or 0.0)
        h = h_pct * page_h if h_pct > 0 else (widget_font_size * 1.3)
        w = w_pct * page_w if w_pct > 0 else 0.0
        if ptype == "checkbox":
            _draw_checkbox(canv, x_left, y_top, w, h, page_h, widget_font_size, value)
        elif ptype == "radio":
            _draw_radio(
                canv, x_left, y_top, w, h, page_h, widget_font_size,
                is_selected=_is_truthy(data.get(p["name"], "")),
            )
        else:
            if not value:
                continue
            _draw_text(canv, x_left, y_top, h, page_h, widget_font_size, value)
    canv.showPage()


def _build_overlay(
    source_pages: list[pypdf.PageObject],
    placements: list[dict[str, Any]],
    data: dict[str, Any],
) -> pypdf.PdfReader:
    """Produce a same-sized PDF with one drawn page per source page.

    Placements on pages that don't exist in the source are silently skipped
    (they'll show up in preflight as anchor-orphaned in PR3, but we don't
    want to hard-fail render time).
    """
    # Pages are 1-based in placement records; lookup by (idx + 1).
    by_page: dict[int, list[dict[str, Any]]] = {}
    for p in placements:
        by_page.setdefault(int(p["page"]), []).append(p)

    buf = io.BytesIO()
    # Initial page size is a placeholder; per-page we call setPageSize.
    canv = canvas.Canvas(buf, pagesize=letter)
    for idx, src_page in enumerate(source_pages):
        page_w = float(src_page.mediabox.width)
        page_h = float(src_page.mediabox.height)
        _draw_overlay_page(canv, page_w, page_h, by_page.get(idx + 1, []), data)
    canv.save()
    buf.seek(0)
    return pypdf.PdfReader(buf)


def render_pdf_template(
    source_pdf_bytes: bytes,
    placements: list[dict[str, Any]],
    data: dict[str, Any],
) -> bytes:
    """Merge the value overlay onto the source PDF and return flat PDF bytes."""
    try:
        source = pypdf.PdfReader(io.BytesIO(source_pdf_bytes))
    except Exception as e:
        raise PdfRenderError(f"Could not open source PDF: {e}") from e

    if source.is_encrypted:
        raise PdfRenderError(
            "Source PDF is encrypted — preflight should have caught this."
        )

    source_pages = list(source.pages)
    overlay = _build_overlay(source_pages, placements, data)
    overlay_pages = list(overlay.pages)

    writer = pypdf.PdfWriter()
    for idx, src_page in enumerate(source_pages):
        # Strip form widget annotations so the output is truly flat — any
        # interactive fields on the source would otherwise sit on top of our
        # drawn values.
        annots = src_page.get("/Annots")
        if annots is not None:
            kept = [
                ref for ref in annots
                if ref.get_object().get("/Subtype") != "/Widget"
            ]
            if kept:
                src_page[pypdf.generic.NameObject("/Annots")] = ArrayObject(kept)
            else:
                del src_page[pypdf.generic.NameObject("/Annots")]

        if idx < len(overlay_pages):
            src_page.merge_page(overlay_pages[idx])
        writer.add_page(src_page)

    # Drop the AcroForm dictionary at the document level too, so PDF readers
    # don't advertise the (now non-existent) fillable form.
    if "/AcroForm" in writer._root_object:
        del writer._root_object["/AcroForm"]

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def execute_pdf_render(
    tv, data: dict[str, Any], output_format: str, *, settings: Settings
) -> bytes:
    """Entry point used by execute_merge dispatch for kind='pdf' templates."""
    if output_format not in ("pdf", None, ""):
        raise PdfRenderError(
            f"PDF templates can only produce PDF output, not {output_format!r}."
        )
    raw = tv.pdf_field_placements or {}
    placements = raw.get("placements") or []
    if not placements:
        # Flat PDFs uploaded without placements can't render — the editor is
        # a hard prerequisite. Surfaces as a clear error rather than a blank
        # overlay so callers know what's wrong.
        raise PdfRenderError(
            "PDF template has no field placements — open it in the editor "
            "and place fields before rendering."
        )
    source_bytes = get_object(settings.s3_bucket_templates, tv.blob_key)
    return render_pdf_template(source_bytes, placements, data)


def persist_pdf_result(
    tv, job_id: uuid.UUID, output: bytes, *, settings: Settings
) -> str:
    """PDF-templates always emit PDF; separate helper keeps the ext hardcoded."""
    result_key = f"renders/{tv.template_id}/v{tv.version}/{job_id}.pdf"
    put_object(settings.s3_bucket_documents, result_key, output, content_type="application/pdf")
    return result_key
