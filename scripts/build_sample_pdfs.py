"""Regenerate the bundled AcroForm + flat PDF sample templates.

Two single-page PDFs mirroring the content of denial_letter.docx so the
three "Or try a sample" buttons on the empty state give operators the
same denial-letter across all three template kinds.

Run:
    python scripts/build_sample_pdfs.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_DIR = Path(__file__).resolve().parents[1] / "sample_templates"
ACROFORM_PATH = OUT_DIR / "denial_letter_acroform.pdf"
FLAT_PATH = OUT_DIR / "denial_letter_flat.pdf"

TITLE = "Claim Denial Notice"
BODY = "We have reviewed claim"
BODY2 = "submitted on"
BODY3 = "for the amount of US$"
BODY4 = "After review, we are unable to process payment."
APPEAL = "You have the right to appeal within 60 days. Contact us at"
SIGN_OFF = "Sincerely,"
TITLE_LINE = "Claims Adjudicator"


def _shell(c: canvas.Canvas) -> None:
    """Draw the printed narrative that's the same on both PDFs."""
    c.setFont("Helvetica-Bold", 18)
    c.setFillColor(HexColor("#0f172a"))
    c.drawString(72, 740, TITLE)
    c.setFillColor(black)
    c.setFont("Helvetica", 11)
    c.drawString(72, 710, "Date:")
    c.drawString(72, 675, "Dear")
    c.drawString(72, 640, BODY)
    c.drawString(72, 615, BODY2)
    c.drawString(72, 590, BODY3)
    c.drawString(72, 565, BODY4)
    c.drawString(72, 525, "Reason for denial:")
    c.setFont("Helvetica", 10)
    c.drawString(72, 470, APPEAL)
    c.setFont("Helvetica", 11)
    c.drawString(72, 380, SIGN_OFF)
    c.drawString(72, 320, TITLE_LINE)


def build_acroform() -> bytes:
    """Fillable PDF: text widgets with /TU labels the vision path won't even need."""
    from io import BytesIO

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _shell(c)
    form = c.acroForm

    def tf(name: str, tooltip: str, x: float, y: float, w: float, h: float = 16) -> None:
        form.textfield(
            name=name, tooltip=tooltip,
            x=x, y=y, width=w, height=h,
            borderStyle="underlined", forceBorder=True,
            fontSize=10,
        )

    tf("letter_date", "Letter Date", 110, 706, 140)
    tf("member_name", "Member Name", 105, 671, 300)
    tf("claim_id", "Claim ID", 175, 636, 120)
    tf("claim_date", "Claim Date", 145, 611, 120)
    tf("amount", "Amount", 240, 586, 120)
    tf("denial_reason", "Reason for Denial", 175, 521, 300)
    tf("appeal_phone", "Appeal Phone", 340, 466, 140)
    tf("agent_name", "Agent Name", 72, 350, 260)

    c.save()
    return buf.getvalue()


def build_flat() -> bytes:
    """Flat PDF: printed narrative + drawn underlines the flat detector picks up."""
    from io import BytesIO

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _shell(c)

    c.setStrokeColor(black)
    c.setLineWidth(0.5)

    def line(x0: float, y: float, x1: float) -> None:
        c.line(x0, y, x1, y)

    line(110, 704, 250)
    line(105, 669, 405)
    line(175, 634, 295)
    line(145, 609, 265)
    line(240, 584, 360)
    line(175, 519, 475)
    line(340, 464, 480)
    line(72, 348, 332)

    c.save()
    return buf.getvalue()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ACROFORM_PATH.write_bytes(build_acroform())
    print(f"wrote {ACROFORM_PATH} ({ACROFORM_PATH.stat().st_size} bytes)")
    FLAT_PATH.write_bytes(build_flat())
    print(f"wrote {FLAT_PATH} ({FLAT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
