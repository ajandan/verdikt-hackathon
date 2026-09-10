"""End-to-end tests for kind='pdf' templates.

Covers the AcroForm-derived path: build a fillable PDF, preflight it, upload
it (which harvests widget rects into placements), render with a payload, and
verify the merged output is a valid PDF whose form widgets have been stripped
and whose values are drawn on the source.
"""
from __future__ import annotations

import io
import uuid

import pypdf
import pytest
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from docgen.auth import sign_session
from docgen.core.render_pipeline_pdf import PdfRenderError, render_pdf_template
from docgen.designtime.freeze import freeze_pdf_version
from docgen.designtime.preflight_pdf import preflight_pdf
from docgen.designtime.upload import upload_pdf_template
from docgen.main import app
from docgen.store.db import async_session
from docgen.store.models import Template, TemplateVersion


def _make_acroform_pdf(
    field_names: list[str], tooltips: dict[str, str] | None = None
) -> bytes:
    """Build a tiny PDF with a text-input widget per name, stacked vertically.

    `tooltips` maps field_name → human-readable label (/TU). Defaults to
    tooltip==name so the widget carries a usable label under either key.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    form = c.acroForm
    c.setFont("Helvetica", 12)
    y = 700
    for name in field_names:
        c.drawString(72, y + 15, f"{name}:")
        form.textfield(
            name=name,
            tooltip=(tooltips or {}).get(name, name),
            x=180, y=y, width=280, height=20,
            borderStyle="inset",
            forceBorder=True,
        )
        y -= 40
    c.save()
    return buf.getvalue()


def _make_flat_pdf() -> bytes:
    """PDF with text but no widgets — the 'flat' preflight path."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 12)
    c.drawString(72, 720, "Denial letter — flat template")
    c.drawString(72, 680, "Member name:")
    c.drawString(72, 640, "Claim ID:")
    c.save()
    return buf.getvalue()


def _text_from_pdf(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def test_preflight_pdf_rejects_non_pdf() -> None:
    r = preflight_pdf(b"not a pdf")
    assert r.outcome == "reject"
    assert any(f.code == "pdf.invalid" for f in r.findings)


def test_preflight_pdf_rejects_empty() -> None:
    r = preflight_pdf(b"")
    assert r.outcome == "reject"
    assert any(f.code == "pdf.empty" for f in r.findings)


def test_preflight_pdf_acroform_harvests_placements() -> None:
    pdf = _make_acroform_pdf(["member_name", "claim_id", "amount"])
    r = preflight_pdf(pdf)
    assert r.outcome == "accept"
    assert r.flavor == "acroform"
    assert r.page_count == 1
    names = {p["name"] for p in r.initial_placements}
    assert names == {"member_name", "claim_id", "amount"}
    for p in r.initial_placements:
        # Fractions in [0, 1]; page numbers 1-based (single-page PDF).
        assert 0 <= p["x_pct"] <= 1
        assert 0 <= p["y_pct"] <= 1
        assert p["w_pct"] > 0
        assert p["page"] == 1
        assert p["anchor"] is None
        # Label is seeded from the widget's /TU tooltip (falls back to /T).
        assert p["label"] == p["name"]


def test_preflight_pdf_flat_returns_empty_placements() -> None:
    r = preflight_pdf(_make_flat_pdf())
    assert r.outcome == "accept"
    assert r.flavor == "flat"
    assert r.initial_placements == []


def _make_tree_acroform_pdf() -> bytes:
    """Build an AcroForm where fields and widgets are separate objects.

    Adobe Acrobat/LiveCycle produce this shape: /AcroForm/Fields points at
    field dicts that carry /T and /FT, and each field has a /Kids array of
    widget annotations that only hold /Rect + /Subtype=/Widget. Reportlab's
    canvas.acroForm cannot produce this — it always merges field+widget into
    one dict — so we assemble it manually via pypdf's writer.
    """
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        RectangleObject,
        TextStringObject,
    )

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    def _widget(rect: tuple[float, float, float, float]) -> DictionaryObject:
        w = DictionaryObject()
        w[NameObject("/Type")] = NameObject("/Annot")
        w[NameObject("/Subtype")] = NameObject("/Widget")
        w[NameObject("/Rect")] = RectangleObject([FloatObject(v) for v in rect])
        w[NameObject("/P")] = page.indirect_reference
        return w

    def _field(name: str, ft: str, widget_rect: tuple[float, float, float, float]) -> DictionaryObject:
        widget = _widget(widget_rect)
        widget_ref = writer._add_object(widget)
        f = DictionaryObject()
        f[NameObject("/T")] = TextStringObject(name)
        f[NameObject("/FT")] = NameObject(ft)
        f[NameObject("/TU")] = TextStringObject(name.replace("_", " ").title())
        f[NameObject("/Kids")] = ArrayObject([widget_ref])
        return f, widget_ref

    member_field, member_widget_ref = _field("member_name", "/Tx", (72, 700, 360, 720))
    claim_field, claim_widget_ref = _field("claim_id", "/Tx", (72, 660, 360, 680))
    consent_field, consent_widget_ref = _field("consent", "/Btn", (72, 620, 90, 638))

    member_ref = writer._add_object(member_field)
    claim_ref = writer._add_object(claim_field)
    consent_ref = writer._add_object(consent_field)

    page[NameObject("/Annots")] = ArrayObject([member_widget_ref, claim_widget_ref, consent_widget_ref])

    acro_form = DictionaryObject()
    acro_form[NameObject("/Fields")] = ArrayObject([member_ref, claim_ref, consent_ref])
    acro_form[NameObject("/NeedAppearances")] = NumberObject(1)
    writer._root_object[NameObject("/AcroForm")] = acro_form

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_preflight_pdf_tree_acroform_harvests_placements() -> None:
    """Adobe-shape AcroForms (field carries /T, widget kid carries /Rect) must
    yield placements — before the field-tree walker, harvesting only worked on
    reportlab's merged field/widget shape and skipped these entirely."""
    pdf = _make_tree_acroform_pdf()
    r = preflight_pdf(pdf)
    assert r.outcome == "accept"
    assert r.flavor == "acroform"
    names = {p["name"] for p in r.initial_placements}
    assert names == {"member_name", "claim_id", "consent"}
    by_name = {p["name"]: p for p in r.initial_placements}
    assert by_name["member_name"]["type"] == "text"
    assert by_name["claim_id"]["type"] == "text"
    assert by_name["consent"]["type"] == "checkbox"
    for p in r.initial_placements:
        assert p["page"] == 1
        assert 0 <= p["x_pct"] <= 1
        assert 0 <= p["y_pct"] <= 1
        assert p["w_pct"] > 0
        # /TU tooltip on the parent field flows through as the label.
        assert p["label"] == p["name"].replace("_", " ").title()


def test_render_pdf_draws_values_and_strips_widgets() -> None:
    """The rendered output should contain payload values and no form widgets."""
    pdf = _make_acroform_pdf(["member_name", "claim_id"])
    report = preflight_pdf(pdf)
    output = render_pdf_template(
        pdf,
        report.initial_placements,
        {"member_name": "Jane Doe", "claim_id": "C-12345"},
    )
    assert output.startswith(b"%PDF-")
    text = _text_from_pdf(output)
    assert "Jane Doe" in text
    assert "C-12345" in text

    # Widgets must be gone from the output — a re-parse should show zero
    # /Widget annotations and no /AcroForm at the document level.
    out_reader = pypdf.PdfReader(io.BytesIO(output))
    assert "/AcroForm" not in out_reader.trailer["/Root"]
    for page in out_reader.pages:
        annots = page.get("/Annots") or []
        for a in annots:
            assert a.get_object().get("/Subtype") != "/Widget"


def test_preflight_stamps_type_on_harvested_placements() -> None:
    """Harvested placements must carry an explicit `type` so the render
    pipeline doesn't fall back to the legacy field_type inference."""
    pdf = _make_acroform_pdf(["member_name"])
    r = preflight_pdf(pdf)
    assert r.outcome == "accept"
    assert r.initial_placements[0]["type"] == "text"


def _flat_pdf_with_checkbox_placement(*, value: object, use_legacy_shape: bool = False) -> bytes:
    """Render a single checkbox on a flat page. `use_legacy_shape` mirrors a
    placement frozen before the `type` field existed (field_type only)."""
    placement: dict = {
        "name": "agree",
        "page": 1,
        "x_pct": 0.5,
        "y_pct": 0.5,
        "w_pct": 0.03,
        "h_pct": 0.03,
        "font_size": 12.0,
        "anchor": None,
    }
    if use_legacy_shape:
        placement["field_type"] = "checkbox"
    else:
        placement["type"] = "checkbox"
    return render_pdf_template(_make_flat_pdf(), [placement], {"agree": value})


def _stroke_ops_present(pdf_bytes: bytes) -> bool:
    """True when at least one stroked rectangle appears anywhere in the doc.

    Uses pdfplumber's parsed rect primitives rather than a raw content-stream
    regex so we don't get fooled by "re" occurring inside a compressed text
    stream.
    """
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for r in page.rects:
                if r.get("stroke"):
                    return True
    return False


def test_render_pdf_checkbox_draws_only_glyph_when_truthy() -> None:
    """Truthy checkbox → just the X glyph, no outline.

    The outline used to draw unconditionally, but on AcroForm PDFs the source
    already draws its own frame in the page content stream, so our overlay
    produced a doubled border in prod output. Flat PDFs are accepted as a
    tradeoff — an unchecked cell shows nothing.
    """
    output = _flat_pdf_with_checkbox_placement(value=True)
    assert "X" in _text_from_pdf(output)
    assert not _stroke_ops_present(output), "checkbox should not draw its own frame"


def test_render_pdf_checkbox_falsy_draws_nothing() -> None:
    """Unchecked checkboxes draw nothing at all — no frame, no glyph."""
    output = _flat_pdf_with_checkbox_placement(value=False)
    assert not _stroke_ops_present(output), "unchecked checkbox should draw no frame"
    assert "X" not in _text_from_pdf(output)


def test_render_pdf_legacy_placement_without_type_still_renders_as_checkbox() -> None:
    """Frozen versions from before the `type` field must keep rendering as
    checkboxes when the legacy field_type='checkbox' signal is set."""
    output = _flat_pdf_with_checkbox_placement(value=True, use_legacy_shape=True)
    assert "X" in _text_from_pdf(output)


def _curve_drawing_present(pdf_bytes: bytes, *, filled: bool) -> bool:
    """True when any drawing contains a bezier curve with the given fill.

    reportlab's `canv.circle()` decomposes into 4 bezier segments. pdfplumber
    surfaces those as `page.curves` entries with `stroke` and `fill` flags,
    and the raw `path` list contains at least one `('c', ...)` operator.
    The rectangle-check used for checkboxes would false-negative for radios,
    hence a separate helper.
    """
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for c in page.curves:
                if filled:
                    if not c.get("fill"):
                        continue
                else:
                    if not c.get("stroke"):
                        continue
                path = c.get("path") or ()
                if any(op[0] == "c" for op in path):
                    return True
    return False


def _flat_pdf_with_radio_placement(*, value: object) -> bytes:
    placement = {
        "name": "opt_a",
        "type": "radio",
        "page": 1,
        "x_pct": 0.5,
        "y_pct": 0.5,
        "w_pct": 0.03,
        "h_pct": 0.03,
        "font_size": 12.0,
        "anchor": None,
    }
    return render_pdf_template(_make_flat_pdf(), [placement], {"opt_a": value})


def test_render_pdf_radio_draws_outline_when_unselected() -> None:
    """Unselected radios draw only the outline circle — no filled dot.

    Same rationale as the empty checkbox: the reader must be able to see
    there's a radio there even when nothing is picked.
    """
    output = _flat_pdf_with_radio_placement(value=False)
    assert _curve_drawing_present(output, filled=False), "radio outline missing"
    assert not _curve_drawing_present(output, filled=True), "unselected radio drew a filled dot"
    # And no rect — checkbox dispatch would draw one.
    assert not _stroke_ops_present(output), "radio dispatched to checkbox path"


def test_render_pdf_radio_draws_filled_dot_when_selected() -> None:
    """Selected radios: outline circle + a smaller filled dot inside."""
    output = _flat_pdf_with_radio_placement(value=True)
    assert _curve_drawing_present(output, filled=False), "radio outline missing"
    assert _curve_drawing_present(output, filled=True), "selected radio missing filled dot"


def test_render_pdf_radio_reuses_checkbox_truthy_rules() -> None:
    """Radios must agree with checkboxes on what "selected" means — "Yes",
    "true", "1", and bool True all light the dot; empty / "no" / False don't."""
    for truthy in (True, "true", "yes", "1", "Y", "on", 1):
        out = _flat_pdf_with_radio_placement(value=truthy)
        assert _curve_drawing_present(out, filled=True), f"{truthy!r} should select radio"
    for falsy in (False, "", "no", "0", "off", None):
        out = _flat_pdf_with_radio_placement(value=falsy)
        assert not _curve_drawing_present(out, filled=True), f"{falsy!r} should not select radio"


def test_render_pdf_text_placement_draws_no_frame() -> None:
    """Text placements should not accidentally get boxed by the new dispatch."""
    placement = {
        "name": "member_name",
        "type": "text",
        "page": 1,
        "x_pct": 0.2,
        "y_pct": 0.5,
        "w_pct": 0.4,
        "h_pct": 0.03,
        "font_size": 12.0,
        "anchor": None,
    }
    output = render_pdf_template(_make_flat_pdf(), [placement], {"member_name": "Jane Doe"})
    assert not _stroke_ops_present(output), "text placement drew a frame — dispatch bug"
    assert "Jane Doe" in _text_from_pdf(output)


def test_render_pdf_without_placements_errors() -> None:
    """Flat PDFs uploaded but never edited must error clearly at render time."""

    class _TV:
        blob_key = "unused"
        pdf_field_placements = {"placements": []}
        pdf_flavor = "flat"

    with pytest.raises(PdfRenderError, match="no field placements"):
        # Direct call bypasses blob fetch — placements empty triggers early error.
        from docgen.core.render_pipeline_pdf import execute_pdf_render
        execute_pdf_render(_TV(), {"any": "thing"}, "pdf", settings=None)


@pytest.mark.asyncio
async def test_upload_pdf_template_end_to_end(default_tenant: uuid.UUID) -> None:
    """Uploading a fillable PDF should create a kind='pdf' template with
    auto-harvested placements ready to render."""
    pdf = _make_acroform_pdf(["member_name", "claim_id"])
    async with async_session() as session:
        template, tv, report = await upload_pdf_template(
            session,
            name=f"pdf-e2e-{uuid.uuid4().hex[:6]}",
            pdf_bytes=pdf,
            tenant_id=default_tenant,
        )
        assert template.kind == "pdf"
        assert tv.pdf_flavor == "acroform"
        assert report.outcome == "accept"
        placements = (tv.pdf_field_placements or {}).get("placements") or []
        assert {p["name"] for p in placements} == {"member_name", "claim_id"}
        # Schema fields mirror the placements so downstream mapping sees them.
        assert {f["name"] for f in tv.schema["fields"]} == {"member_name", "claim_id"}

    # Cleanup so the test is idempotent across reruns. Template.versions has
    # cascade="all, delete-orphan" so the version row goes with it.
    async with async_session() as session:
        obj = await session.get(Template, template.id)
        if obj is not None:
            await session.delete(obj)
            await session.commit()


async def _cleanup_template(template_id: uuid.UUID) -> None:
    async with async_session() as session:
        obj = await session.get(Template, template_id)
        if obj is not None:
            await session.delete(obj)
            await session.commit()


@pytest.mark.asyncio
async def test_freeze_pdf_happy_path(default_tenant: uuid.UUID) -> None:
    """Freeze an acroform-flavored PDF: schema derived from placements,
    version_hash computed, is_frozen flag flipped, blob rehosted."""
    pdf = _make_acroform_pdf(["member_name", "claim_id"])
    async with async_session() as session:
        template, tv, _ = await upload_pdf_template(
            session,
            name=f"pdf-freeze-{uuid.uuid4().hex[:6]}",
            pdf_bytes=pdf,
            tenant_id=default_tenant,
        )
        version_id = tv.id
        template_id = template.id

    try:
        async with async_session() as session:
            frozen = await freeze_pdf_version(session, version_id=version_id)
            assert frozen.is_frozen is True
            assert frozen.status == "frozen"
            assert frozen.version_hash and len(frozen.version_hash) == 64
            assert frozen.blob_key.startswith(f"frozen/{template_id}/")
            assert {f["name"] for f in frozen.schema["fields"]} == {
                "member_name", "claim_id"
            }
    finally:
        await _cleanup_template(template_id)


@pytest.mark.asyncio
async def test_freeze_pdf_rejects_empty_placements(default_tenant: uuid.UUID) -> None:
    """Flat PDF with no placements can't be frozen — user must run the editor first."""
    async with async_session() as session:
        template, tv, _ = await upload_pdf_template(
            session,
            name=f"pdf-flat-{uuid.uuid4().hex[:6]}",
            pdf_bytes=_make_flat_pdf(),
            tenant_id=default_tenant,
        )
        version_id = tv.id
        template_id = template.id
        assert tv.pdf_flavor == "flat"
        assert (tv.pdf_field_placements or {}).get("placements", []) == []

    try:
        async with async_session() as session:
            with pytest.raises(ValueError, match="no field placements"):
                await freeze_pdf_version(session, version_id=version_id)
    finally:
        await _cleanup_template(template_id)


@pytest.mark.asyncio
async def test_freeze_pdf_dedupes_shared_names(default_tenant: uuid.UUID) -> None:
    """Multiple widgets sharing a name (radio-button groups per PDF spec)
    collapse to one schema field but keep all widget positions in
    placements so the renderer stamps every location."""
    async with async_session() as session:
        template, tv, _ = await upload_pdf_template(
            session,
            name=f"pdf-dup-{uuid.uuid4().hex[:6]}",
            pdf_bytes=_make_flat_pdf(),
            tenant_id=default_tenant,
        )
        version_id = tv.id
        template_id = template.id

        # Two widgets, same name — the AcroForm equivalent of a radio group.
        tv.pdf_field_placements = {
            "placements": [
                {"name": "dup", "page": 1, "x_pct": 0.1, "y_pct": 0.1,
                 "w_pct": 0.2, "h_pct": 0.02, "font_size": 11, "anchor": None},
                {"name": "dup", "page": 1, "x_pct": 0.1, "y_pct": 0.2,
                 "w_pct": 0.2, "h_pct": 0.02, "font_size": 11, "anchor": None},
            ]
        }
        await session.commit()

    try:
        async with async_session() as session:
            frozen = await freeze_pdf_version(session, version_id=version_id)
            assert frozen.is_frozen
            # Schema: one field for the shared name.
            names = [f["name"] for f in (frozen.schema or {}).get("fields", [])]
            assert names == ["dup"]
            # Placements: both widget positions preserved for rendering.
            widgets = (frozen.pdf_field_placements or {}).get("placements", [])
            assert len(widgets) == 2
    finally:
        await _cleanup_template(template_id)


@pytest.mark.asyncio
async def test_render_endpoint_422_when_flat_has_no_placements(
    default_tenant: uuid.UUID, auth_headers: dict[str, str]
) -> None:
    """REST /render must fail fast with 422 before enqueuing a doomed job."""
    # Prep: upload flat PDF, hand-freeze bypassing the "no placements" guard
    # by seeding one placement, then blank the list so the frozen row has
    # neither placements nor Jinja tags to fall back on.
    async with async_session() as session:
        template, tv, _ = await upload_pdf_template(
            session,
            name=f"pdf-empty-render-{uuid.uuid4().hex[:6]}",
            pdf_bytes=_make_flat_pdf(),
            tenant_id=default_tenant,
        )
        template_id = template.id
        # Seed → freeze → then blank the placements to simulate a corrupt
        # frozen row. Renders must still surface 422, not 500.
        tv.pdf_field_placements = {
            "placements": [
                {"name": "x", "page": 1, "x_pct": 0.1, "y_pct": 0.1,
                 "w_pct": 0.2, "h_pct": 0.02, "font_size": 11, "anchor": None},
            ]
        }
        await session.commit()

        frozen = await freeze_pdf_version(session, version_id=tv.id)
        template.current_version = frozen.version
        # Blank placements after freeze — models allow the mutation; the
        # render entry point is the last line of defense.
        row = await session.get(TemplateVersion, frozen.id)
        row.pdf_field_placements = {"placements": []}
        await session.commit()

    try:
        with TestClient(app) as client:
            # Payload satisfies the frozen schema (field "x" is required), so
            # validate_payload passes — the placements guard is what rejects.
            r = client.post(
                "/render",
                headers=auth_headers,
                json={"template_id": str(template_id), "data": {"x": "anything"}},
            )
        assert r.status_code == 422, r.text
        assert "no field placements" in r.text.lower()
    finally:
        await _cleanup_template(template_id)


@pytest.mark.asyncio
async def test_pdf_page_preview_endpoint_returns_png(
    default_tenant: uuid.UUID,
) -> None:
    """UI /pdf-page/{n}.png endpoint renders + caches a page thumbnail."""
    async with async_session() as session:
        template, tv, _ = await upload_pdf_template(
            session,
            name=f"pdf-preview-{uuid.uuid4().hex[:6]}",
            pdf_bytes=_make_acroform_pdf(["a"]),
            tenant_id=default_tenant,
        )
        version_id = tv.id
        template_id = template.id

    try:
        client = TestClient(app)
        client.cookies.set("docgen_session", sign_session("admin"))
        r = client.get(f"/ui/versions/{version_id}/pdf-page/1.png")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        await _cleanup_template(template_id)


@pytest.mark.asyncio
async def test_placements_save_persists(default_tenant: uuid.UUID) -> None:
    """POST /versions/{id}/placements replaces the placement list."""
    async with async_session() as session:
        template, tv, _ = await upload_pdf_template(
            session,
            name=f"pdf-save-{uuid.uuid4().hex[:6]}",
            pdf_bytes=_make_flat_pdf(),
            tenant_id=default_tenant,
        )
        version_id = tv.id
        template_id = template.id

    try:
        client = TestClient(app)
        client.cookies.set("docgen_session", sign_session("admin"))
        # CSRF middleware wants an X-CSRF-Token that matches the cookie value;
        # test client won't have the cookie set by a login flow, so we set
        # both sides to the same value.
        client.cookies.set("docgen_csrf", "test-csrf-token")
        new_placements = [
            {"name": "member_name", "page": 1, "x_pct": 0.15, "y_pct": 0.22,
             "w_pct": 0.4, "font_size": 12, "anchor": "top-left"},
            {"name": "claim_id", "page": 1, "x_pct": 0.15, "y_pct": 0.30,
             "w_pct": 0.3, "font_size": 12, "anchor": "top-left"},
        ]
        r = client.post(
            f"/ui/versions/{version_id}/placements",
            headers={"X-CSRF-Token": "test-csrf-token"},
            json={"placements": new_placements},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert {p["name"] for p in body["placements"]} == {"member_name", "claim_id"}

        async with async_session() as session:
            row = await session.get(TemplateVersion, version_id)
            saved = (row.pdf_field_placements or {}).get("placements", [])
            assert {p["name"] for p in saved} == {"member_name", "claim_id"}
            # Schema mirrored so downstream mapping views stay in sync.
            assert {f["name"] for f in row.schema["fields"]} == {"member_name", "claim_id"}
    finally:
        await _cleanup_template(template_id)


@pytest.mark.asyncio
async def test_pdf_preview_merged_endpoint(default_tenant: uuid.UUID) -> None:
    """Preview-at-freeze: /pdf-preview.pdf renders draft placements over the source."""
    async with async_session() as session:
        template, tv, _ = await upload_pdf_template(
            session,
            name=f"pdf-preview-merged-{uuid.uuid4().hex[:6]}",
            pdf_bytes=_make_acroform_pdf(["member_name", "claim_id"]),
            tenant_id=default_tenant,
        )
        version_id = tv.id
        template_id = template.id

    try:
        client = TestClient(app)
        client.cookies.set("docgen_session", sign_session("admin"))
        r = client.get(f"/ui/versions/{version_id}/pdf-preview.pdf")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF-")
        text = _text_from_pdf(r.content)
        # Each placement's own name (bracketed) should be drawn on the page.
        assert "[member_name]" in text
        assert "[claim_id]" in text
    finally:
        await _cleanup_template(template_id)


# The ai-map endpoint was removed in cefdb07 once label-driven payload
# keys made the server-side slugging redundant. Test intentionally deleted.


@pytest.mark.asyncio
async def test_pdf_preview_merged_rejects_empty(default_tenant: uuid.UUID) -> None:
    """Preview endpoint 422s cleanly when the user hasn't added any placements."""
    async with async_session() as session:
        template, tv, _ = await upload_pdf_template(
            session,
            name=f"pdf-preview-empty-{uuid.uuid4().hex[:6]}",
            pdf_bytes=_make_flat_pdf(),
            tenant_id=default_tenant,
        )
        version_id = tv.id
        template_id = template.id

    try:
        client = TestClient(app)
        client.cookies.set("docgen_session", sign_session("admin"))
        r = client.get(f"/ui/versions/{version_id}/pdf-preview.pdf")
        assert r.status_code == 422
        assert "no placements" in r.text.lower()
    finally:
        await _cleanup_template(template_id)
