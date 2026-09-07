"""End-to-end coverage for the bundled Explanation of Benefits sample.

The EOB is the demo's "real-world stakes" sample. It exists to prove three
things to a judge in one screen:

    1. Placeholders like `[SERVICE_DATE]` / `[TOTAL_CHARGED]` get typed by the
       keyword heuristic (date, number).
    2. A `{%tr for line in service_lines %}` docxtpl row loop is preflight-
       clean AND `service_lines` gets inferred as a `list` via the AST walker.
    3. An `{% if is_denied %}` conditional makes `is_denied` a `boolean`.

If any of those regress, the demo narrative collapses — so we lock all three
in with tests that read the bundled `.docx` fixture directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from docgen.core.preflight import (
    _extract_document_xml,
    _extract_text_with_tags,
    _normalize_docxtpl_scope_tags,
    _parse_jinja,
    preflight,
)
from docgen.designtime.mapper import _backfill_orphan_suggestions
from docgen.providers.base import MappingProposal
from docgen.providers.stub import StubProvider

_EOB_PATH = Path(__file__).resolve().parents[1] / "sample_templates" / "insurance_eob.docx"


@pytest.fixture(scope="module")
def eob_bytes() -> bytes:
    if not _EOB_PATH.exists():
        pytest.skip("insurance_eob.docx not bundled — run scripts/build_eob_docx.py")
    return _EOB_PATH.read_bytes()


def test_normalize_docxtpl_scope_tags_strips_tr_tc_p_r() -> None:
    """docxtpl's row/col/paragraph/run prefixes must not confuse Jinja."""
    src = (
        "{%tr for x in xs %}A{%tr endfor %}"
        "{%tc if flag %}B{%tc endif %}"
        "{%p for y in ys %}C{%p endfor %}"
        "{%r if bold %}D{%r endif %}"
    )
    out = _normalize_docxtpl_scope_tags(src)
    # The `tr`/`tc`/`p`/`r` prefix is gone; the Jinja keyword survives.
    assert "tr for" not in out and "tc if" not in out
    assert "{% for x in xs %}" in out
    assert "{% if flag %}" in out
    assert "{% endfor %}" in out


def test_eob_preflight_accepts(eob_bytes: bytes) -> None:
    """The sample must clear preflight — otherwise the demo's first click errors."""
    report = preflight(eob_bytes)
    assert report.outcome in {"accept", "accept-with-warnings"}, (
        f"EOB rejected: {[f.message for f in report.findings]}"
    )
    # service_lines / is_denied are the AST-inference showpieces; both must be
    # visible to the parser after docxtpl-tag normalization.
    assert {"is_denied", "service_lines"}.issubset(set(report.detected_variables))


@pytest.mark.asyncio
async def test_eob_mapper_flow_produces_expected_field_types(eob_bytes: bytes) -> None:
    """Full mapper flow: stub proposes typed scalars; backfill types the loop and flag."""
    # Extract text the same way mapper does.
    text = _extract_text_with_tags(_extract_document_xml(eob_bytes))
    proposal = await StubProvider().propose_mapping(text)
    _backfill_orphan_suggestions(proposal, text)

    by_name = {s.field.name: s for s in proposal.suggestions}

    # Scalars: keyword heuristics
    assert by_name["service_date"].field.type == "date"
    assert by_name["processed_date"].field.type == "date"
    assert by_name["total_charged"].field.type == "number"
    assert by_name["total_allowed"].field.type == "number"
    assert by_name["total_paid"].field.type == "number"
    assert by_name["member_name"].field.type == "string"

    # AST inference — this is the load-bearing demo moment
    assert by_name["service_lines"].field.type == "list"
    assert by_name["service_lines"].source == "orphan_backfill"
    assert by_name["is_denied"].field.type == "boolean"
    assert by_name["is_denied"].source == "orphan_backfill"

    # Every row must carry a "Why:" — the reasoning column in the wizard would
    # be empty otherwise and the demo point about explainability falls flat.
    for s in proposal.suggestions:
        assert s.reasoning, f"missing reasoning for {s.field.name}"


def test_eob_is_registered_as_a_sample_kind() -> None:
    """UI wiring: routes must expose an 'eob' sample kind pointing at this fixture."""
    from docgen.ui.routes import _SAMPLE_KINDS

    assert "eob" in _SAMPLE_KINDS
    filename, _display = _SAMPLE_KINDS["eob"]
    assert filename == "insurance_eob.docx"
