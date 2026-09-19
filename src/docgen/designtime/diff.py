"""Diff two frozen template versions — schema deltas + tagged-DOCX text diff.

The admin UI uses this to preview what actually changed before publishing or
rolling back. Diffing the *tagged* (post-freeze) .docx is what matters: it's
the artifact the renderer will consume, so any surprise there is a rendering
surprise. Draft blobs are excluded — they're the raw upload without jinja
tags injected, and diffing them against a frozen sibling would be misleading.
"""
from __future__ import annotations

import difflib
import io
import zipfile
from dataclasses import dataclass, field
from typing import Any

from docgen.core.preflight import W_NS
from docgen.core.xml_safe import safe_fromstring

_DOC_XML = "word/document.xml"


@dataclass
class FieldChange:
    name: str
    kind: str  # "type_changed" | "required_changed"
    from_value: str
    to_value: str


@dataclass
class SchemaDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[FieldChange] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.added or self.removed or self.changed)


@dataclass
class TemplateDiff:
    schema: SchemaDiff
    text_diff_lines: list[str]
    text_identical: bool


def _fields_by_name(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in schema.get("fields", []) or []:
        n = f.get("name")
        if isinstance(n, str) and n:
            out[n] = f
    return out


def diff_schemas(
    from_schema: dict[str, Any], to_schema: dict[str, Any]
) -> SchemaDiff:
    fa = _fields_by_name(from_schema)
    fb = _fields_by_name(to_schema)
    added = sorted(set(fb) - set(fa))
    removed = sorted(set(fa) - set(fb))
    common = sorted(set(fa) & set(fb))
    changed: list[FieldChange] = []
    unchanged: list[str] = []
    for name in common:
        a, b = fa[name], fb[name]
        if a.get("type") != b.get("type"):
            changed.append(FieldChange(
                name=name, kind="type_changed",
                from_value=str(a.get("type")),
                to_value=str(b.get("type")),
            ))
            continue
        if bool(a.get("required")) != bool(b.get("required")):
            changed.append(FieldChange(
                name=name, kind="required_changed",
                from_value="required" if a.get("required") else "optional",
                to_value="required" if b.get("required") else "optional",
            ))
            continue
        unchanged.append(name)
    return SchemaDiff(added=added, removed=removed, changed=changed, unchanged=unchanged)


def _extract_paragraphs(docx_bytes: bytes) -> list[str]:
    """One string per <w:p>, in document order.

    Empty paragraphs are preserved so paragraph indices line up across versions —
    difflib needs stable alignment to produce a readable hunk.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        xml = zf.read(_DOC_XML)
    root = safe_fromstring(xml)
    paragraphs: list[str] = []
    for p in root.iter(f"{{{W_NS}}}p"):
        text = "".join((t.text or "") for t in p.iter(f"{{{W_NS}}}t"))
        # Newlines inside a single <w:t> would confuse difflib's line semantics;
        # strip them so each paragraph occupies exactly one diff "line".
        paragraphs.append(text.replace("\n", " ").replace("\r", " "))
    return paragraphs


def diff_text(
    from_bytes: bytes,
    to_bytes: bytes,
    *,
    from_label: str = "from",
    to_label: str = "to",
) -> tuple[list[str], bool]:
    a = _extract_paragraphs(from_bytes)
    b = _extract_paragraphs(to_bytes)
    if a == b:
        return [], True
    return (
        list(difflib.unified_diff(a, b, fromfile=from_label, tofile=to_label, lineterm="")),
        False,
    )


def compute_template_diff(
    *,
    from_schema: dict[str, Any],
    to_schema: dict[str, Any],
    from_bytes: bytes,
    to_bytes: bytes,
    from_label: str,
    to_label: str,
) -> TemplateDiff:
    schema = diff_schemas(from_schema, to_schema)
    lines, identical = diff_text(
        from_bytes, to_bytes, from_label=from_label, to_label=to_label
    )
    return TemplateDiff(schema=schema, text_diff_lines=lines, text_identical=identical)
