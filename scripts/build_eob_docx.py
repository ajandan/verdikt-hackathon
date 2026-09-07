"""Generate sample_templates/insurance_eob.docx — a realistic EOB fixture.

This is the "real-world stakes" sample used in the demo. It intentionally
exercises three different code paths in the mapping wizard so reviewers see
the reasoning trace do something interesting:

  * Plain placeholders like `[MEMBER_NAME]`, `[SERVICE_DATE]`, `[TOTAL_CHARGED]`
    — the AI / stub provider proposes typed fields (string / date / number).
  * A pre-tagged `{% tr for line in service_lines %}` table row — the orphan-
    backfill pass sees `service_lines` in a `for` iterable and classifies it
    as `list` via the Jinja AST walker.
  * A pre-tagged `{% if is_denied %}` block — orphan-backfill classifies
    `is_denied` as `boolean` (only appears in a conditional test).

Run:
    python scripts/build_eob_docx.py
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

OUT_PATH = Path(__file__).resolve().parents[1] / "sample_templates" / "insurance_eob.docx"


def _add_heading(doc: Document, text: str, size: int, *, bold: bool = True, color: RGBColor | None = None) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color


def _labeled(doc: Document, label: str, placeholder: str) -> None:
    """Emit `Label: [PLACEHOLDER]` on one paragraph — the layout the stub
    provider was designed against."""
    p = doc.add_paragraph()
    r = p.add_run(f"{label}: ")
    r.bold = True
    p.add_run(placeholder)


def _two_col(doc: Document, left_label: str, left_ph: str, right_label: str, right_ph: str) -> None:
    """Compact two-column identifier line."""
    table = doc.add_table(rows=1, cols=2)
    table.autofit = True
    left, right = table.rows[0].cells
    left.paragraphs[0].add_run(f"{left_label}: ").bold = True
    left.paragraphs[0].add_run(left_ph)
    right.paragraphs[0].add_run(f"{right_label}: ").bold = True
    right.paragraphs[0].add_run(right_ph)


def _service_line_table(doc: Document) -> None:
    """Header row + one docxtpl-templated body row that expands per line item.

    docxtpl expands a row prefixed with `{% tr for ... %}` / `{% tr endfor %}`
    on a paragraph inside that row. Layout mirrors what a real EOB shows to
    a patient.
    """
    headers = ["Code", "Description", "Charged", "Allowed", "Paid", "You owe"]
    table = doc.add_table(rows=3, cols=len(headers))
    table.style = "Light Grid Accent 1"

    # Header row
    for cell, text in zip(table.rows[0].cells, headers, strict=True):
        run = cell.paragraphs[0].add_run(text)
        run.bold = True
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER

    # docxtpl loop row: the {% tr for %} tag goes in the FIRST cell; the
    # matching {% tr endfor %} goes in the LAST cell of a subsequent row.
    body_cells = table.rows[1].cells
    body_cells[0].paragraphs[0].add_run(
        "{%tr for line in service_lines %}{{ line.code }}"
    )
    body_cells[1].paragraphs[0].add_run("{{ line.description }}")
    body_cells[2].paragraphs[0].add_run("${{ line.charged }}")
    body_cells[3].paragraphs[0].add_run("${{ line.allowed }}")
    body_cells[4].paragraphs[0].add_run("${{ line.paid }}")
    body_cells[5].paragraphs[0].add_run("${{ line.owe }}{%tr endfor %}")


def _totals_block(doc: Document) -> None:
    doc.add_paragraph()  # spacer
    table = doc.add_table(rows=4, cols=2)
    table.autofit = True
    rows = [
        ("Total charged", "$[TOTAL_CHARGED]"),
        ("Total allowed by plan", "$[TOTAL_ALLOWED]"),
        ("Total paid to provider", "$[TOTAL_PAID]"),
        ("Your responsibility", "$[PATIENT_RESPONSIBILITY]"),
    ]
    for i, (label, placeholder) in enumerate(rows):
        left, right = table.rows[i].cells
        r = left.paragraphs[0].add_run(label)
        r.bold = i == len(rows) - 1  # bold the final "You owe" line
        rr = right.paragraphs[0].add_run(placeholder)
        rr.bold = i == len(rows) - 1
        right.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT


def _denial_block(doc: Document) -> None:
    """Conditional denial explanation — drives boolean type inference on
    `is_denied` via the AST walker."""
    p = doc.add_paragraph()
    p.add_run("{% if is_denied %}").italic = True

    warn = doc.add_paragraph()
    r = warn.add_run("This claim was DENIED.")
    r.bold = True
    r.font.color.rgb = RGBColor(0xB4, 0x1E, 0x1E)

    reason = doc.add_paragraph()
    reason.add_run("Reason for denial: ").bold = True
    reason.add_run("[DENIAL_REASON]")

    appeal = doc.add_paragraph()
    appeal.add_run(
        "You have the right to appeal this decision within 60 days. "
        "To file an appeal, call [APPEAL_PHONE] or write to the plan at the "
        "address above."
    )

    end = doc.add_paragraph()
    end.add_run("{% endif %}").italic = True


def build() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()

    # Header
    _add_heading(doc, "EXPLANATION OF BENEFITS", 20, color=RGBColor(0x0F, 0x17, 0x2A))
    _add_heading(doc, "This is not a bill — keep for your records.", 10, bold=False)
    doc.add_paragraph()

    # Identifier block
    _two_col(doc, "Member", "[MEMBER_NAME]", "Member ID", "[MEMBER_ID]")
    _two_col(doc, "Plan", "[PLAN_NAME]", "Group number", "[GROUP_NUMBER]")
    _two_col(doc, "Provider", "[PROVIDER_NAME]", "Claim number", "[CLAIM_NUMBER]")
    _two_col(doc, "Service date", "[SERVICE_DATE]", "Date processed", "[PROCESSED_DATE]")
    doc.add_paragraph()

    # Services rendered
    p = doc.add_paragraph()
    r = p.add_run("Services rendered")
    r.bold = True
    r.font.size = Pt(12)
    _service_line_table(doc)

    _totals_block(doc)
    doc.add_paragraph()

    # Denial conditional
    _denial_block(doc)
    doc.add_paragraph()

    # Footer
    footer = doc.add_paragraph()
    footer.add_run(
        "If you have questions about this Explanation of Benefits, call "
        "member services at [MEMBER_SERVICES_PHONE]."
    )

    doc.add_paragraph()
    sig = doc.add_paragraph()
    sig.add_run("Sincerely,").italic = True
    doc.add_paragraph("[PLAN_NAME] Claims Department")

    doc.save(OUT_PATH)
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
