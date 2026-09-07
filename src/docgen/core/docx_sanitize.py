"""Upload-time guard for DOCX bytes.

Two jobs:
  1. Reject non-DOCX payloads early — Content-Type headers are trivially
     forgeable, so we verify the actual bytes look like a Word 2007+
     package (ZIP magic + `[Content_Types].xml` present).
  2. Strip VBA macro payloads. A well-formed macro-enabled document
     lives in `.docm`, not `.docx`; a `.docx` with `word/vbaProject.bin`
     is either a mis-saved template or a small piece of social
     engineering (rename `.docm` → `.docx`, hope the operator opens it
     locally to eyeball the fields). Stripping is safer than rejecting
     — the document body is intact, only the macros go.

Called from `designtime/upload.py` before `preflight()`. Returns the
sanitized bytes plus a small report the caller stashes in the audit
event.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

_ZIP_MAGIC = b"PK\x03\x04"
_CONTENT_TYPES_PART = "[Content_Types].xml"
# Every part inside the archive that contributes to macro execution.
# Rewriting `[Content_Types].xml` to drop the VBA content type would be
# nice, but Word is happy to open a .docx whose content-types manifest
# references a missing part — cleaner to leave the manifest alone and
# just delete the bytes.
_MACRO_PARTS: frozenset[str] = frozenset({
    "word/vbaProject.bin",
    "word/vbaData.xml",
})


class InvalidDocxError(ValueError):
    """Raised when the uploaded bytes aren't a plausible DOCX package."""


@dataclass
class SanitizeReport:
    macros_stripped: list[str]

    def to_dict(self) -> dict[str, list[str]]:
        return {"macros_stripped": list(self.macros_stripped)}


def sanitize_docx(data: bytes) -> tuple[bytes, SanitizeReport]:
    """Verify the bytes are a DOCX and strip any macro parts.

    Raises `InvalidDocxError` when the bytes aren't a ZIP or lack the
    core `[Content_Types].xml` manifest — those are hard rejects, the
    caller should return 400 to the operator.

    Returns `(clean_bytes, report)`. When the input had no macros the
    returned bytes are byte-identical to the input (no re-pack cost);
    only rebuild the archive when we actually removed something."""
    if not data.startswith(_ZIP_MAGIC):
        raise InvalidDocxError(
            "Uploaded file is not a valid DOCX (ZIP magic bytes missing). "
            "Save the file as .docx from Word before uploading."
        )

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if _CONTENT_TYPES_PART not in names:
                raise InvalidDocxError(
                    "Uploaded file is a ZIP archive but not a DOCX package "
                    f"(missing {_CONTENT_TYPES_PART})."
                )
            macro_parts = [n for n in names if n in _MACRO_PARTS]
            if not macro_parts:
                return data, SanitizeReport(macros_stripped=[])

            # Repack without the macro parts. Preserve compression + date
            # per entry so the archive stays deterministic-ish (helps
            # dedup on content_hash for the same document uploaded twice).
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as out:
                for info in zf.infolist():
                    if info.filename in _MACRO_PARTS:
                        continue
                    out.writestr(info, zf.read(info.filename))
    except zipfile.BadZipFile as e:
        raise InvalidDocxError(
            f"Uploaded file is not a readable ZIP archive: {e}"
        ) from e

    return buf.getvalue(), SanitizeReport(macros_stripped=sorted(macro_parts))
