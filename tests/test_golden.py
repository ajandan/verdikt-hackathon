"""Golden-file regression net for the deterministic render path (N-5).

'Real templates + expected outputs, structure-compared in CI.'
Every fixture is (template.docx, payload.json, expected.txt). The test renders and
extracts a normalized text-tree view of the output DOCX and asserts it matches.

Structure comparison — not byte comparison — so cosmetic OOXML noise (nsmap ordering,
run splits) doesn't cause false regressions. Content divergence does.
"""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest
from docx import Document

from docgen.core.merge import merge_docx

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
_UPDATE_GOLDENS = os.environ.get("UPDATE_GOLDENS") == "1"


def _extract_structure(docx_bytes: bytes) -> str:
    """Serialize paragraphs + tables into the normalized shape used by expected.txt."""
    doc = Document(io.BytesIO(docx_bytes))
    lines: list[str] = []
    seen_tables: set[int] = set()

    for block in _iter_body(doc):
        if block.__class__.__name__ == "Paragraph":
            text = _norm(block.text)
            if text:
                lines.append(text)
        else:  # Table
            tid = id(block)
            if tid in seen_tables:
                continue
            seen_tables.add(tid)
            lines.append("[TABLE]")
            for row in block.rows:
                cells = [_norm(cell.text) for cell in row.cells]
                lines.append(" | ".join(cells))
            lines.append("[/TABLE]")
    return "\n".join(lines) + "\n"


def _iter_body(doc):
    """Iterate paragraphs and tables in document order."""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _cases() -> list[tuple[str, Path]]:
    return sorted(
        (p.name, p) for p in GOLDEN_DIR.iterdir()
        if p.is_dir() and (p / "template.docx").exists()
    )


@pytest.mark.parametrize("name,case_dir", _cases(), ids=lambda x: x if isinstance(x, str) else "")
def test_golden_render(name: str, case_dir: Path) -> None:
    template = (case_dir / "template.docx").read_bytes()
    payload = json.loads((case_dir / "payload.json").read_text(encoding="utf-8"))
    expected = (case_dir / "expected.txt").read_text(encoding="utf-8")

    rendered = merge_docx(template, payload)
    actual = _extract_structure(rendered)

    if actual != expected:
        if _UPDATE_GOLDENS:
            (case_dir / "expected.txt").write_text(actual, encoding="utf-8")
            pytest.skip(f"Updated golden for '{name}' — review the git diff before committing.")
        diff_view = "\n".join(
            f"  {i:3d}: expected={e!r}  actual={a!r}"
            for i, (e, a) in enumerate(
                zip(expected.splitlines(), actual.splitlines())
            )
            if e != a
        )
        extra_expected = expected.splitlines()[len(actual.splitlines()):]
        extra_actual = actual.splitlines()[len(expected.splitlines()):]
        pytest.fail(
            f"Golden mismatch for '{name}':\n{diff_view}\n"
            f"  extra expected: {extra_expected}\n"
            f"  extra actual:   {extra_actual}\n"
            f"If the change is intentional, rerun with UPDATE_GOLDENS=1 pytest tests/test_golden.py "
            f"then commit the updated expected.txt files."
        )
