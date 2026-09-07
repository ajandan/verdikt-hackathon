"""Phase 3 (§diff view): compare two frozen template versions.

Unit-level tests only — the diff helper takes bytes + schemas, so no DB
fixtures are needed.
"""
from __future__ import annotations

import io

from docx import Document

from docgen.designtime.diff import (
    _extract_paragraphs,
    compute_template_diff,
    diff_schemas,
    diff_text,
)


def _docx(paragraphs: list[str]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# diff_schemas
# ---------------------------------------------------------------------------


def test_diff_schemas_identical_produces_no_delta() -> None:
    s = {"fields": [{"name": "a", "type": "string", "required": True}]}
    d = diff_schemas(s, s)
    assert d.added == []
    assert d.removed == []
    assert d.changed == []
    assert d.unchanged == ["a"]
    assert d.has_any is False


def test_diff_schemas_reports_added_and_removed() -> None:
    a = {"fields": [{"name": "a"}, {"name": "b"}]}
    b = {"fields": [{"name": "b"}, {"name": "c"}]}
    d = diff_schemas(a, b)
    assert d.added == ["c"]
    assert d.removed == ["a"]
    assert d.unchanged == ["b"]
    assert d.has_any is True


def test_diff_schemas_flags_type_change() -> None:
    a = {"fields": [{"name": "amount", "type": "string"}]}
    b = {"fields": [{"name": "amount", "type": "number"}]}
    d = diff_schemas(a, b)
    assert d.added == []
    assert d.removed == []
    assert d.unchanged == []
    assert len(d.changed) == 1
    ch = d.changed[0]
    assert ch.name == "amount"
    assert ch.kind == "type_changed"
    assert ch.from_value == "string"
    assert ch.to_value == "number"


def test_diff_schemas_flags_required_toggle() -> None:
    a = {"fields": [{"name": "note", "type": "string", "required": True}]}
    b = {"fields": [{"name": "note", "type": "string", "required": False}]}
    d = diff_schemas(a, b)
    assert len(d.changed) == 1
    assert d.changed[0].kind == "required_changed"
    assert d.changed[0].from_value == "required"
    assert d.changed[0].to_value == "optional"


def test_diff_schemas_type_change_wins_over_required() -> None:
    """When both type and required differ, type_changed is the more urgent
    signal — report it first without also duplicating a required_changed row."""
    a = {"fields": [{"name": "x", "type": "string", "required": True}]}
    b = {"fields": [{"name": "x", "type": "number", "required": False}]}
    d = diff_schemas(a, b)
    assert [c.kind for c in d.changed] == ["type_changed"]


def test_diff_schemas_ignores_malformed_field_entries() -> None:
    a = {"fields": [{"name": "a"}, {}, {"name": None}, {"name": ""}]}
    b = {"fields": [{"name": "a"}]}
    d = diff_schemas(a, b)
    assert d.added == []
    assert d.removed == []
    assert d.unchanged == ["a"]


def test_diff_schemas_handles_empty_and_missing_fields_key() -> None:
    assert diff_schemas({}, {}).has_any is False
    assert diff_schemas({"fields": None}, {"fields": None}).has_any is False


# ---------------------------------------------------------------------------
# diff_text / _extract_paragraphs
# ---------------------------------------------------------------------------


def test_extract_paragraphs_returns_one_per_paragraph_in_order() -> None:
    docx = _docx(["First", "Second", "Third"])
    paras = _extract_paragraphs(docx)
    assert paras == ["First", "Second", "Third"]


def test_diff_text_identical_returns_empty_and_true() -> None:
    docx = _docx(["Hello world"])
    lines, identical = diff_text(docx, docx)
    assert lines == []
    assert identical is True


def test_diff_text_reports_paragraph_change() -> None:
    a = _docx(["Line one", "Line two", "Line three"])
    b = _docx(["Line one", "Line 2", "Line three"])
    lines, identical = diff_text(a, b, from_label="v1", to_label="v2")
    assert identical is False
    # unified_diff includes header lines starting with --- / +++
    joined = "\n".join(lines)
    assert "--- v1" in joined
    assert "+++ v2" in joined
    assert any(line.startswith("-Line two") for line in lines)
    assert any(line.startswith("+Line 2") for line in lines)
    # Untouched paragraphs stay as context (leading space)
    assert any(line == " Line one" for line in lines)


def test_diff_text_handles_paragraph_added() -> None:
    a = _docx(["A", "C"])
    b = _docx(["A", "B", "C"])
    lines, identical = diff_text(a, b)
    assert identical is False
    assert any(line == "+B" for line in lines)


# ---------------------------------------------------------------------------
# compute_template_diff
# ---------------------------------------------------------------------------


def test_compute_template_diff_combines_schema_and_text() -> None:
    a_bytes = _docx(["Hello {{ name }}."])
    b_bytes = _docx(["Hello {{ name }}, welcome."])
    a_schema = {"fields": [{"name": "name", "type": "string"}]}
    b_schema = {
        "fields": [
            {"name": "name", "type": "string"},
            {"name": "note", "type": "string", "required": False},
        ]
    }
    td = compute_template_diff(
        from_schema=a_schema,
        to_schema=b_schema,
        from_bytes=a_bytes,
        to_bytes=b_bytes,
        from_label="v1",
        to_label="v2",
    )
    assert td.schema.added == ["note"]
    assert td.schema.removed == []
    assert td.text_identical is False
    assert any(line.startswith("+Hello {{ name }}, welcome.") for line in td.text_diff_lines)


def test_compute_template_diff_no_changes_when_identical() -> None:
    docx = _docx(["Same"])
    schema = {"fields": [{"name": "x"}]}
    td = compute_template_diff(
        from_schema=schema, to_schema=schema,
        from_bytes=docx, to_bytes=docx,
        from_label="a", to_label="b",
    )
    assert td.schema.has_any is False
    assert td.text_identical is True
    assert td.text_diff_lines == []
