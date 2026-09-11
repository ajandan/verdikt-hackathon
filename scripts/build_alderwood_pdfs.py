"""Regenerate the three Alderwood-branded PAP reply letters (approve / deny /
missing_info) that Verdikt renders after the reason stage emits a Decision.

Each PDF carries fillable AcroForm text widgets whose /TU tooltips match the
Decision + Intake field names, so Quire's designtime labeler resolves them
without needing the vision path. The chrome (logo, section bars, footer) is
shared across all three so the fictional Alderwood brand reads consistently
in the demo.

Run:
    python scripts/build_alderwood_pdfs.py
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_DIR = Path(__file__).resolve().parents[1] / "sample_templates" / "alderwood"

# Brand palette lifted straight from the enrollment form (ASP-100) so the
# outbound reply letters read as the same product family as the intake form.
TEAL = HexColor("#175F5B")
TEAL_DARK = HexColor("#0F4A47")
FIELD_FILL = HexColor("#F2C4B0")   # coral form-field fill from ASP-100
FIELD_BORDER = HexColor("#E9B39B")  # slightly deeper coral for widget borders
BOX_FILL = HexColor("#FBE6DA")     # pale coral for the header contact box
INK = HexColor("#0F172A")
MUTED = HexColor("#54706E")

PAGE_W, PAGE_H = letter
MARGIN = 54


def _draw_logo(c: canvas.Canvas, cx: float, cy: float, r: float = 14) -> None:
    """Vector approximation of the tree-in-circle mark on ASP-100."""
    c.setFillColor(TEAL)
    c.setStrokeColor(TEAL)
    c.circle(cx, cy, r, stroke=0, fill=1)
    c.setFillColor(white)
    # Trunk
    c.rect(cx - 1.2, cy - r * 0.55, 2.4, r * 0.35, stroke=0, fill=1)
    # Three stacked triangles as foliage.
    layers = [
        (r * 0.85, r * 0.55, r * 0.20),
        (r * 0.65, r * 0.55, r * 0.55),
        (r * 0.50, r * 0.55, r * 0.85),
    ]
    for width, height, top_offset in layers:
        base_y = cy - r * 0.20 + (top_offset - r * 0.85) * 0.6
        path = c.beginPath()
        path.moveTo(cx - width, base_y)
        path.lineTo(cx + width, base_y)
        path.lineTo(cx, base_y + height)
        path.close()
        c.drawPath(path, stroke=0, fill=1)


def _draw_header(c: canvas.Canvas) -> None:
    """Logo + wordmark + tagline row that opens every letter.

    Vertical rhythm (top is PAGE_H - MARGIN):
      top-2   → wordmark baseline (large breathing room above)
      top-18  → tagline baseline (14pt gap)
      top-58  → separator line (24pt clear below the contact box bottom)
    """
    top = PAGE_H - MARGIN
    _draw_logo(c, MARGIN + 16, top - 14, r=18)

    # Reserve the top-right for the contact box; tagline stops before it.
    box_w = 176
    right_col_left = PAGE_W - MARGIN - box_w - 12

    wordmark_x = MARGIN + 50
    c.setFillColor(TEAL_DARK)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(wordmark_x, top - 6, "Alderwood")
    alderwood_w = c.stringWidth("Alderwood", "Helvetica-Bold", 20)
    c.setFillColor(TEAL)
    c.setFont("Helvetica", 14)
    c.drawString(wordmark_x + alderwood_w + 8, top - 6, "Specialty Pharmacy")

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    tagline = (
        "Hub Services  ·  Benefits Investigation  ·  Copay & Patient Assistance"
    )
    # Guard: if the tagline would collide with the contact box, drop the last
    # bullet segment. Cheap belt-and-braces so a future rename can't re-break it.
    while (
        c.stringWidth(tagline, "Helvetica", 8) > right_col_left - wordmark_x - 8
        and "·" in tagline
    ):
        tagline = tagline.rsplit("·", 1)[0].rstrip()
    c.drawString(wordmark_x, top - 22, tagline)

    # Right-side contact block, mirrored from the enrollment form.
    box_x, box_y, box_h = PAGE_W - MARGIN - box_w, top - 32, 40
    c.setStrokeColor(FIELD_BORDER)
    c.setLineWidth(0.8)
    c.setFillColor(BOX_FILL)
    c.rect(box_x, box_y, box_w, box_h, stroke=1, fill=1)
    c.setFillColor(TEAL_DARK)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(box_x + 12, box_y + box_h - 12, "PATIENT SUPPORT")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(box_x + 12, box_y + box_h - 26, "1-877-555-0100")
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(box_x + 12, box_y + 6, "Mon–Fri  8am–8pm ET")

    # Separator sits 24pt below the contact box bottom so it never grazes it.
    c.setStrokeColor(TEAL)
    c.setLineWidth(0.6)
    c.line(MARGIN, top - 58, PAGE_W - MARGIN, top - 58)


def _draw_footer(c: canvas.Canvas, form_code: str) -> None:
    """Confidentiality + sample-template disclosure + URL/fax.

    Right column reserved so long left-side disclaimers can't crash into the
    form code / URL row like the first draft did.
    """
    c.setStrokeColor(TEAL)
    c.setLineWidth(0.4)
    c.line(MARGIN, 60, PAGE_W - MARGIN, 60)

    right_col_w = 170
    left_edge = MARGIN
    right_edge_for_left = PAGE_W - MARGIN - right_col_w - 10

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.5)
    line1 = (
        "CONFIDENTIAL — Protected health information intended only for the named recipient."
    )
    line2 = "SAMPLE TEMPLATE — Alderwood is fictional; not for clinical use."
    # Clip each line to the reserved left column so nothing overruns.
    max_left = right_edge_for_left - left_edge
    while c.stringWidth(line1, "Helvetica", 7.5) > max_left and " " in line1:
        line1 = line1.rsplit(" ", 1)[0]
    while c.stringWidth(line2, "Helvetica", 7.5) > max_left and " " in line2:
        line2 = line2.rsplit(" ", 1)[0]
    c.drawString(left_edge, 48, line1)
    c.drawString(left_edge, 38, line2)

    c.setFont("Helvetica-Bold", 7.5)
    c.drawRightString(PAGE_W - MARGIN, 48, form_code)
    c.setFont("Helvetica", 7.5)
    c.drawRightString(PAGE_W - MARGIN, 38, "alderwoodrx.example  ·  Fax 1-877-555-0142")


def _field_underline(c: canvas.Canvas, x: float, y: float, w: float) -> None:
    """Thin coral bottom rule instead of a filled rectangle.

    A boxed fill baked into the page content stream reads fine on the empty
    template preview but muddies the merged output — values sit inside a
    coral slab. An underline gives the same visual affordance for empty
    fields while letting rendered values read on white, which is how real
    notification letters look.
    """
    c.saveState()
    c.setStrokeColor(FIELD_BORDER)
    c.setLineWidth(0.6)
    c.line(x, y, x + w, y)
    c.restoreState()


def _tf(
    c: canvas.Canvas,
    name: str,
    tooltip: str,
    x: float,
    y: float,
    w: float,
    h: float = 16,
) -> None:
    """Underlined AcroForm text field. Widget itself is transparent so the
    merged value sits cleanly on the coral rule with no boxy background."""
    _field_underline(c, x, y, w)
    c.acroForm.textfield(
        name=name,
        tooltip=tooltip,
        x=x,
        y=y,
        width=w,
        height=h,
        borderStyle="solid",
        borderWidth=0,
        borderColor=FIELD_BORDER,
        fillColor=None,
        textColor=INK,
        forceBorder=False,
        fontSize=10,
    )


def _multiline(
    c: canvas.Canvas,
    name: str,
    tooltip: str,
    x: float,
    y: float,
    w: float,
    h: float,
) -> None:
    """Full-width multi-line block. No background, no border — the small-caps
    label above identifies the section, and merged prose reads on white."""
    c.acroForm.textfield(
        name=name,
        tooltip=tooltip,
        x=x,
        y=y,
        width=w,
        height=h,
        borderStyle="solid",
        borderWidth=0,
        borderColor=FIELD_BORDER,
        fillColor=None,
        textColor=INK,
        forceBorder=False,
        fontSize=10,
        fieldFlags="multiline",
    )


CONTENT_W = PAGE_W - 2 * MARGIN


def _prose(c: canvas.Canvas, x: float, y: float, w: float, text: str) -> float:
    """Word-wrapped prose paragraph, 10pt Helvetica, 13pt leading.
    Returns y-cursor 8pt below the last baseline (paragraph gap)."""
    c.setFillColor(INK)
    c.setFont("Helvetica", 10)
    words = text.split()
    line: list[str] = []
    cy = y
    for word in words:
        cand = " ".join([*line, word])
        if c.stringWidth(cand, "Helvetica", 10) <= w:
            line.append(word)
            continue
        c.drawString(x, cy, " ".join(line))
        line = [word]
        cy -= 13
    if line:
        c.drawString(x, cy, " ".join(line))
    return cy - 8


def _addressee(c: canvas.Canvas, top: float) -> float:
    """Date (right) + Patient Name + Address + Re line. Reads like a letter opening."""
    # Right-aligned date chip with muted label above it.
    date_w = 132
    date_x = PAGE_W - MARGIN - date_w
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    c.drawRightString(PAGE_W - MARGIN, top + 4, "Letter date")
    _tf(c, "letter_date", "Letter Date", date_x, top - 12, date_w, h=14)

    y = top - 40
    _tf(c, "patient_name", "Patient Name", MARGIN, y, 320, h=15)
    y -= 22
    _multiline(c, "patient_address", "Patient Mailing Address", MARGIN, y - 30, 320, 34)
    y -= 46

    # Re line: program + drug + referral id sit on the same visual row.
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(MARGIN, y, "Re:")
    re_x = MARGIN + c.stringWidth("Re:  ", "Helvetica-Bold", 10.5)
    _tf(c, "program_name", "Program Name", re_x, y - 4, 240, h=14)
    _tf(c, "prescribed_drug", "Prescribed Drug", re_x + 250, y - 4, CONTENT_W - (re_x - MARGIN) - 250, h=14)
    y -= 22
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 9.5)
    c.drawString(re_x, y, "Referral #")
    ref_x = re_x + c.stringWidth("Referral #  ", "Helvetica", 9.5)
    _tf(c, "referral_id", "Referral ID", ref_x, y - 3, 150, h=14)
    return y - 24


def _labeled_multiline(
    c: canvas.Canvas,
    y: float,
    label: str,
    name: str,
    tooltip: str,
    h: float,
) -> float:
    """Small caps label above a full-width coral block. Reads as a headed sub-section."""
    c.setFillColor(TEAL_DARK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(MARGIN, y, label.upper())
    block_top = y - 4
    block_bottom = block_top - h
    _multiline(c, name, tooltip, MARGIN, block_bottom, CONTENT_W, h)
    return block_bottom - 14


def _inline_row(
    c: canvas.Canvas,
    y: float,
    entries: list[tuple[str, str, str, float]],
) -> float:
    """A row of (label, name, tooltip, width) inline coral chips at label-baseline y."""
    x = MARGIN
    for label, name, tooltip, width in entries:
        c.setFillColor(TEAL_DARK)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(x, y, label.upper())
        _tf(c, name, tooltip, x, y - 18, width, h=14)
        x += width + 20
    return y - 34


def _signature(c: canvas.Canvas, y: float) -> None:
    """Sincerely + coordinator block. Reads as a letter closing, not a form."""
    c.setFillColor(INK)
    c.setFont("Helvetica", 10.5)
    c.drawString(MARGIN, y, "Sincerely,")
    y -= 34
    _tf(c, "coordinator_name", "Care Coordinator Name", MARGIN, y, 260, h=15)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.5)
    c.drawString(MARGIN, y - 12, "Care Coordinator  ·  Alderwood Specialty Pharmacy Hub Services")
    y -= 30
    c.setFillColor(TEAL_DARK)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN, y, "DIRECT LINE")
    _tf(c, "coordinator_phone", "Coordinator Phone", MARGIN, y - 16, 180, h=14)
    c.drawString(MARGIN + 200, y, "SIGNED")
    _tf(c, "signed_on", "Signed On", MARGIN + 200, y - 16, 140, h=14)


def _build_approve() -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _draw_header(c)

    y = _addressee(c, PAGE_H - MARGIN - 72)

    c.setFillColor(INK)
    c.setFont("Helvetica", 10.5)
    c.drawString(MARGIN, y, "Dear Applicant,")
    y -= 20

    y = _prose(c, MARGIN, y, CONTENT_W,
        "We are pleased to confirm your enrollment in the Alderwood-administered Patient "
        "Assistance Program named above. Your application has been reviewed against the "
        "program's eligibility rules and approved. Coverage begins on the effective date "
        "noted below and continues for the coverage period shown.")

    y = _inline_row(c, y - 6, [
        ("Coverage effective", "effective_date", "Coverage Effective Date", 150),
        ("Coverage period", "coverage_period", "Coverage Period", CONTENT_W - 150 - 20),
    ])

    y = _labeled_multiline(c, y, "Reviewer rationale",
                           "decision_rationale", "Decision Rationale (Decision.rationale)", h=30)
    y = _labeled_multiline(c, y, "Program rules considered",
                           "rules_matched", "Rules Matched (Decision.citations, matched=true)", h=44)
    y = _labeled_multiline(c, y, "What happens next",
                           "next_steps", "Next Steps for Patient", h=42)

    y = _prose(c, MARGIN, y - 2, CONTENT_W,
        "If you have questions about this decision or your medication, please contact your "
        "care coordinator using the information below.")

    _signature(c, y - 16)
    _draw_footer(c, "APV-201 (Rev. 09/2026)")
    c.save()
    return buf.getvalue()


def _build_deny() -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _draw_header(c)

    y = _addressee(c, PAGE_H - MARGIN - 72)

    c.setFillColor(INK)
    c.setFont("Helvetica", 10.5)
    c.drawString(MARGIN, y, "Dear Applicant,")
    y -= 20

    y = _prose(c, MARGIN, y, CONTENT_W,
        "We have completed our review of your application to the Alderwood-administered "
        "Patient Assistance Program named above. After weighing the submitted information "
        "against the program's eligibility rules, we are unable to enroll you at this time. "
        "This letter explains the basis for that decision and your right to appeal.")

    y = _labeled_multiline(c, y - 4, "Reviewer rationale",
                           "decision_rationale", "Decision Rationale (Decision.rationale)", h=34)
    y = _labeled_multiline(c, y, "Program rules that were not met",
                           "rules_not_matched", "Rules Not Matched (Decision.citations, matched=false)", h=48)

    y = _prose(c, MARGIN, y - 2, CONTENT_W,
        "You have the right to appeal this decision. To do so, follow the instructions below "
        "by the appeal deadline noted. Requests received after the deadline cannot be considered "
        "under this program cycle.")

    y = _inline_row(c, y - 6, [
        ("Appeal deadline", "appeal_deadline", "Appeal Deadline", 150),
        ("Appeal contact", "appeal_contact", "Appeal Contact", CONTENT_W - 150 - 20),
    ])

    y = _labeled_multiline(c, y, "How to file an appeal",
                           "appeal_instructions", "Appeal Instructions", h=44)

    _signature(c, y - 12)
    _draw_footer(c, "DEN-202 (Rev. 09/2026)")
    c.save()
    return buf.getvalue()


def _build_missing_info() -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _draw_header(c)

    y = _addressee(c, PAGE_H - MARGIN - 72)

    c.setFillColor(INK)
    c.setFont("Helvetica", 10.5)
    c.drawString(MARGIN, y, "Dear Applicant,")
    y -= 20

    y = _prose(c, MARGIN, y, CONTENT_W,
        "Thank you for submitting your application to the Alderwood-administered Patient "
        "Assistance Program named above. Before we can complete our review and issue a "
        "decision, we need a small number of additional items from you. Please return them "
        "by the response deadline noted; late submissions restart the review clock.")

    y = _labeled_multiline(c, y - 2, "Reviewer note",
                           "decision_rationale", "Reviewer Note (Decision.rationale)", h=30)
    y = _labeled_multiline(c, y, "Items we still need from you",
                           "missing_items", "Missing Items (Decision.missing_fields)", h=64)

    y = _inline_row(c, y, [
        ("Response deadline", "response_deadline", "Response Deadline", 150),
        ("Return by fax", "return_fax", "Return Fax", 130),
        ("Return by portal", "return_portal", "Return Portal URL", CONTENT_W - 150 - 130 - 40),
    ])

    y = _labeled_multiline(c, y, "How to submit",
                           "how_to_submit", "How To Submit Missing Items", h=40)

    _signature(c, y - 8)
    _draw_footer(c, "REQ-203 (Rev. 09/2026)")
    c.save()
    return buf.getvalue()


BUILDERS = {
    "alderwood_pap_approve.pdf": _build_approve,
    "alderwood_pap_deny.pdf": _build_deny,
    "alderwood_pap_missing_info.pdf": _build_missing_info,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, build in BUILDERS.items():
        path = OUT_DIR / name
        path.write_bytes(build())
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
