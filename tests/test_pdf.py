"""PDF conversion tests — skipped if LibreOffice is not on PATH.

We do not compare PDF bytes (metadata timestamps and object numbering vary run-to-run).
The contract is: the output is a well-formed PDF that contains the expected text.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from docgen.core.merge import merge_docx
from docgen.core.pdf import PdfConversionError, docx_to_pdf

GOLDEN_ROOT = Path(__file__).parent / "fixtures" / "golden"

soffice_available = shutil.which("soffice") or shutil.which("libreoffice")
skip_no_soffice = pytest.mark.skipif(
    not soffice_available, reason="LibreOffice (soffice/libreoffice) not on PATH"
)


def test_pdf_conversion_error_when_binary_missing(monkeypatch) -> None:
    """When neither `soffice` nor `libreoffice` is on PATH, raise PdfConversionError."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(PdfConversionError, match="not found on PATH"):
        docx_to_pdf(b"\x00")


@skip_no_soffice
def test_denial_letter_renders_to_pdf() -> None:
    fixture = GOLDEN_ROOT / "denial_letter"
    template_bytes = (fixture / "template.docx").read_bytes()
    payload = __import__("json").loads((fixture / "payload.json").read_text(encoding="utf-8"))

    docx = merge_docx(template_bytes, payload)
    pdf = docx_to_pdf(docx, timeout_seconds=90.0)

    assert pdf.startswith(b"%PDF-"), "Output is not a PDF (missing magic bytes)"
    assert len(pdf) > 1000, f"PDF suspiciously small ({len(pdf)} bytes)"
