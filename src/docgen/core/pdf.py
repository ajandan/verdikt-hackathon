"""DOCX → PDF conversion via headless LibreOffice.

Kept in `core/` because it is a pure post-render transform: (docx bytes) → (pdf bytes).
No AI, no web, no state. Boundary test guarantees it stays that way.

LibreOffice is invoked in a fresh per-conversion `--user-profile`; without this, concurrent
invocations against the same default profile deadlock. This is the "LibreOffice pool"
pattern from §13 without an explicit pool manager — the OS-level isolation is enough.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


class PdfConversionError(RuntimeError):
    """Raised when LibreOffice fails to convert a docx to pdf."""


def _find_soffice() -> str:
    for candidate in ("soffice", "libreoffice"):
        path = shutil.which(candidate)
        if path:
            return path
    raise PdfConversionError(
        "LibreOffice not found on PATH (looked for 'soffice' and 'libreoffice'). "
        "Install libreoffice-core / libreoffice in the container."
    )


def docx_to_pdf(docx_bytes: bytes, *, timeout_seconds: float = 60.0) -> bytes:
    """Convert a DOCX byte-string to a PDF byte-string.

    Deterministic wrt input: given the same docx, LibreOffice produces the same PDF
    (modulo metadata timestamps). We accept that: bytewise reproducibility is not the
    contract — deterministic *content* is.
    """
    soffice = _find_soffice()
    with tempfile.TemporaryDirectory(prefix="docgen-pdf-") as td:
        tdp = Path(td)
        in_path = tdp / f"in-{uuid.uuid4().hex}.docx"
        out_dir = tdp / "out"
        out_dir.mkdir()
        profile_dir = tdp / "profile"
        in_path.write_bytes(docx_bytes)

        cmd = [
            soffice,
            f"-env:UserInstallation=file://{profile_dir.as_posix()}",
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(in_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise PdfConversionError(f"LibreOffice conversion timed out after {timeout_seconds}s") from e

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
            raise PdfConversionError(f"LibreOffice exited {proc.returncode}: {stderr}")

        pdfs = list(out_dir.glob("*.pdf"))
        if not pdfs:
            raise PdfConversionError(
                "LibreOffice reported success but produced no PDF file "
                f"(stderr: {proc.stderr.decode('utf-8', errors='replace')[:200]})"
            )
        return pdfs[0].read_bytes()
