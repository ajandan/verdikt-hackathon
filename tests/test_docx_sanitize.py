"""Unit tests for upload-time DOCX guard.

Pure zip surgery — no DB, no FastAPI, no python-docx round-trip. Kept
minimal because the function's contract is tiny: reject non-ZIP, reject
missing manifest, strip macro parts, leave everything else alone.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from docgen.core.docx_sanitize import InvalidDocxError, sanitize_docx


def _minimal_docx(*, extra_parts: dict[str, bytes] | None = None) -> bytes:
    """The smallest zip that passes the "looks like a DOCX" check.

    Real Word documents have dozens of parts; sanitize_docx only cares
    that `[Content_Types].xml` exists at the archive root, so we skip
    the rest. `extra_parts` lets tests inject macro payloads (or any
    other named part) without pulling in a full python-docx fixture."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", b"<Types/>")
        for name, data in (extra_parts or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_rejects_non_zip_bytes() -> None:
    """Renamed .exe / plain text / random noise — the magic-byte check
    fires before we open the archive, so this is O(1) not O(size)."""
    with pytest.raises(InvalidDocxError, match="ZIP magic"):
        sanitize_docx(b"not a zip file at all")


def test_rejects_zip_without_content_types_manifest() -> None:
    """A ZIP file dropped in via the docx upload widget — real ZIP magic,
    but not a DOCX package (no [Content_Types].xml). Common failure
    mode when users compress a folder of Word docs into a .zip and
    rename the outer archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", b"hi")
    with pytest.raises(InvalidDocxError, match="Content_Types"):
        sanitize_docx(buf.getvalue())


def test_rejects_corrupt_zip() -> None:
    """Bytes that pass the magic-byte check but the central directory is
    truncated (partial upload, aborted transfer). Central directory read
    happens on ZipFile open, which raises BadZipFile → we re-raise as
    InvalidDocxError so the upload handler flashes a friendly message."""
    # PK\x03\x04 header but nothing else — no central directory, no EOCD.
    with pytest.raises(InvalidDocxError, match="readable ZIP"):
        sanitize_docx(b"PK\x03\x04\x00\x00garbage")


def test_passes_through_when_no_macros() -> None:
    """No macros = byte-identical output. We skip the repack cost so a
    plain DOCX round-trips through sanitize_docx for free (matters
    because the caller hashes the bytes for the blob key)."""
    data = _minimal_docx()
    out, report = sanitize_docx(data)
    assert out is data  # identity, not just equality
    assert report.macros_stripped == []


def test_strips_vba_project_bin() -> None:
    """The interesting case: a .docx with word/vbaProject.bin (a .docm
    renamed to .docx). The macro part is removed, other parts stay."""
    data = _minimal_docx(extra_parts={
        "word/vbaProject.bin": b"MACRO_PAYLOAD",
        "word/document.xml": b"<w:document/>",
    })
    out, report = sanitize_docx(data)
    assert report.macros_stripped == ["word/vbaProject.bin"]
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        names = zf.namelist()
        assert "word/vbaProject.bin" not in names
        assert "word/document.xml" in names
        assert "[Content_Types].xml" in names


def test_strips_all_known_macro_parts() -> None:
    """Both vbaProject.bin and its sidecar vbaData.xml go — leaving one
    without the other is worse than either (Word may pop a repair
    dialog if it sees an orphan)."""
    data = _minimal_docx(extra_parts={
        "word/vbaProject.bin": b"payload",
        "word/vbaData.xml": b"<vba/>",
    })
    _out, report = sanitize_docx(data)
    assert sorted(report.macros_stripped) == ["word/vbaData.xml", "word/vbaProject.bin"]


def test_leaves_unrelated_word_parts_alone() -> None:
    """Anything not on the macro list stays put — no over-eager stripping
    of files that happen to live under word/ (styles, numbering,
    settings, custom XML, etc.)."""
    data = _minimal_docx(extra_parts={
        "word/vbaProject.bin": b"payload",
        "word/styles.xml": b"<w:styles/>",
        "word/numbering.xml": b"<w:numbering/>",
        "customXml/item1.xml": b"<x/>",
    })
    out, _ = sanitize_docx(data)
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        names = set(zf.namelist())
    assert "word/vbaProject.bin" not in names
    assert "word/styles.xml" in names
    assert "word/numbering.xml" in names
    assert "customXml/item1.xml" in names
