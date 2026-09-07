from __future__ import annotations

import io
import zipfile

import pytest
from docx import Document

from docgen.core.merge import RenderError, merge_docx
from docgen.designtime.freeze import _apply_replacements_to_document_xml, _rewrite_docx


def _rendered_text(docx_bytes: bytes) -> str:
    doc = Document(io.BytesIO(docx_bytes))
    return "\n".join(p.text for p in doc.paragraphs)


def test_apply_replacements_paragraph_level(sample_docx_bytes: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(sample_docx_bytes)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    new_xml = _apply_replacements_to_document_xml(
        xml,
        {"[MEMBER_NAME]": "{{ member_name }}", "[CLAIM_ID]": "{{ claim_id }}"},
    )
    assert "{{ member_name }}" in new_xml
    assert "{{ claim_id }}" in new_xml
    assert "[MEMBER_NAME]" not in new_xml


def test_rewrite_and_merge_end_to_end(sample_docx_bytes: bytes) -> None:
    tagged = _rewrite_docx(
        sample_docx_bytes,
        {
            "[MEMBER_NAME]": "{{ member_name }}",
            "[CLAIM_ID]": "{{ claim_id }}",
            "[CLAIM_DATE]": "{{ claim_date }}",
            "[AMOUNT]": "{{ amount }}",
            "<<agent_name>>": "{{ agent_name }}",
        },
    )
    rendered = merge_docx(
        tagged,
        {
            "member_name": "Alice Example",
            "claim_id": "CLM-42",
            "claim_date": "2026-08-01",
            "amount": "1234.56",
            "agent_name": "B. Jones",
        },
    )
    text = _rendered_text(rendered)
    assert "Alice Example" in text
    assert "CLM-42" in text
    assert "1234.56" in text
    assert "B. Jones" in text
    assert "[MEMBER_NAME]" not in text


def test_sandbox_blocks_sandbox_escape_chain(sample_docx_bytes: bytes) -> None:
    # Single-hop `.__class__` renders as Undefined (empty). The real escape vector
    # is chained attribute access to reach __subclasses__ — that must raise.
    tagged = _rewrite_docx(
        sample_docx_bytes,
        {"[MEMBER_NAME]": "{{ member.__class__.__mro__[1].__subclasses__() }}"},
    )
    with pytest.raises(RenderError) as exc:
        merge_docx(tagged, {"member": {"name": "x"}})
    assert "unsafe" in str(exc.value).lower() or "SecurityError" in str(exc.value)


def test_sandbox_allows_normal_attribute_access(sample_docx_bytes: bytes) -> None:
    tagged = _rewrite_docx(sample_docx_bytes, {"[MEMBER_NAME]": "{{ member.name }}"})

    class Obj:
        name = "Alice"

    rendered = merge_docx(tagged, {"member": Obj()})
    assert "Alice" in _rendered_text(rendered)
