"""Seed Alderwood-branded static guideline PDFs, doc packages, and program bindings.

Idempotent — every step upserts by name/slug so re-running is safe. Populates
the demo tenant's static-doc library with four guideline PDFs (Alderwood
brand chrome, no AcroForm fields, byte-stable), assembles three doc packages
(one per outcome) that mix the outcome-matched reply template with the
relevant guideline PDFs, and wires the CardioVita program's outcome
bindings to those packages.

Post-run state:
  * StaticDocuments        4 (guideline PDFs)
  * DocPackages            3 (approve / deny / missing_info bundles)
  * Program.outcomes       ["approve", "deny", "missing_info"]
  * Program.package_bindings  each outcome >> its bundle

Run:
    python scripts/seed_alderwood_doc_packages.py
Optional env:
    ALDERWOOD_TENANT_NAME  override the tenant name (default: "Alderwood Specialty Pharmacy")
    ALDERWOOD_PROGRAM_SLUG override the program slug (default: "cardiovita-pap-2026")
"""
from __future__ import annotations

import asyncio
import os
import sys
from io import BytesIO
from pathlib import Path

# Ensure `src/` is importable when the script is run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from docgen.config import get_settings
from docgen.store.doc_packages import (
    add_static_document_version,
    create_doc_package,
    create_static_document,
    normalize_package_items,
)
from docgen.store.models import (
    DocPackage,
    Program,
    StaticDocument,
    Template,
    Tenant,
)

# --------------------------------------------------------------------------
# Alderwood brand tokens — copied from scripts/build_alderwood_pdfs.py so the
# guideline PDFs read as the same product family as the reply letters.
# --------------------------------------------------------------------------
TEAL = HexColor("#175F5B")
TEAL_DARK = HexColor("#0F4A47")
BOX_FILL = HexColor("#FBE6DA")
FIELD_BORDER = HexColor("#E9B39B")
INK = HexColor("#0F172A")
MUTED = HexColor("#54706E")

PAGE_W, PAGE_H = letter
MARGIN = 54
CONTENT_W = PAGE_W - 2 * MARGIN


def _draw_logo(c: canvas.Canvas, cx: float, cy: float, r: float = 14) -> None:
    c.setFillColor(TEAL)
    c.circle(cx, cy, r, stroke=0, fill=1)
    c.setFillColor(white)
    c.rect(cx - 1.2, cy - r * 0.55, 2.4, r * 0.35, stroke=0, fill=1)
    for width, height, top_offset in (
        (r * 0.85, r * 0.55, r * 0.20),
        (r * 0.65, r * 0.55, r * 0.55),
        (r * 0.50, r * 0.55, r * 0.85),
    ):
        base_y = cy - r * 0.20 + (top_offset - r * 0.85) * 0.6
        p = c.beginPath()
        p.moveTo(cx - width, base_y)
        p.lineTo(cx + width, base_y)
        p.lineTo(cx, base_y + height)
        p.close()
        c.drawPath(p, stroke=0, fill=1)


def _draw_header(c: canvas.Canvas, subtitle: str) -> None:
    top = PAGE_H - MARGIN
    _draw_logo(c, MARGIN + 16, top - 14, r=18)
    c.setFillColor(TEAL_DARK)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(MARGIN + 50, top - 6, "Alderwood")
    w = c.stringWidth("Alderwood", "Helvetica-Bold", 20)
    c.setFillColor(TEAL)
    c.setFont("Helvetica", 14)
    c.drawString(MARGIN + 50 + w + 8, top - 6, "Specialty Pharmacy")
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    c.drawString(MARGIN + 50, top - 22, subtitle)
    c.setStrokeColor(TEAL)
    c.setLineWidth(0.6)
    c.line(MARGIN, top - 40, PAGE_W - MARGIN, top - 40)


def _draw_footer(c: canvas.Canvas, form_code: str, page_no: int, page_count: int) -> None:
    c.setStrokeColor(TEAL)
    c.setLineWidth(0.4)
    c.line(MARGIN, 60, PAGE_W - MARGIN, 60)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawString(
        MARGIN,
        48,
        "CONFIDENTIAL — For the named recipient's use in the Alderwood assistance program.",
    )
    c.drawString(MARGIN, 38, "SAMPLE ENCLOSURE — Alderwood is a fictional demo brand.")
    c.setFont("Helvetica-Bold", 7.5)
    c.drawRightString(PAGE_W - MARGIN, 48, form_code)
    c.setFont("Helvetica", 7.5)
    c.drawRightString(
        PAGE_W - MARGIN,
        38,
        f"alderwoodrx.example  -  Page {page_no} of {page_count}",
    )


def _wrap(c: canvas.Canvas, text: str, w: float, font: str, size: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    line: list[str] = []
    for word in words:
        cand = " ".join([*line, word])
        if c.stringWidth(cand, font, size) <= w:
            line.append(word)
            continue
        lines.append(" ".join(line))
        line = [word]
    if line:
        lines.append(" ".join(line))
    return lines


def _draw_h1(c: canvas.Canvas, y: float, text: str) -> float:
    c.setFillColor(TEAL_DARK)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(MARGIN, y, text)
    return y - 26


def _draw_h2(c: canvas.Canvas, y: float, text: str) -> float:
    c.setFillColor(TEAL_DARK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN, y, text.upper())
    c.setStrokeColor(FIELD_BORDER)
    c.setLineWidth(0.5)
    c.line(MARGIN, y - 4, MARGIN + 40, y - 4)
    return y - 20


def _draw_prose(c: canvas.Canvas, y: float, text: str) -> float:
    c.setFillColor(INK)
    c.setFont("Helvetica", 10)
    for line in _wrap(c, text, CONTENT_W, "Helvetica", 10):
        c.drawString(MARGIN, y, line)
        y -= 13
    return y - 8


def _draw_bullets(c: canvas.Canvas, y: float, items: list[str]) -> float:
    c.setFillColor(INK)
    c.setFont("Helvetica", 10)
    for item in items:
        wrapped = _wrap(c, item, CONTENT_W - 18, "Helvetica", 10)
        c.setFillColor(TEAL)
        c.circle(MARGIN + 4, y + 3, 1.5, stroke=0, fill=1)
        c.setFillColor(INK)
        for i, line in enumerate(wrapped):
            c.drawString(MARGIN + 14, y, line)
            y -= 13
        y -= 4
    return y - 4


def _draw_callout(c: canvas.Canvas, y: float, title: str, body: str) -> float:
    """Coral-tinted callout box — same treatment as the letter contact box."""
    lines = _wrap(c, body, CONTENT_W - 24, "Helvetica", 9.5)
    box_h = 22 + len(lines) * 13
    box_top = y
    box_bottom = box_top - box_h
    c.setStrokeColor(FIELD_BORDER)
    c.setLineWidth(0.8)
    c.setFillColor(BOX_FILL)
    c.rect(MARGIN, box_bottom, CONTENT_W, box_h, stroke=1, fill=1)
    c.setFillColor(TEAL_DARK)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(MARGIN + 12, box_top - 14, title.upper())
    c.setFillColor(INK)
    c.setFont("Helvetica", 9.5)
    cy = box_top - 28
    for line in lines:
        c.drawString(MARGIN + 12, cy, line)
        cy -= 13
    return box_bottom - 14


# --------------------------------------------------------------------------
# Section blocks — each guideline PDF is a short list of blocks.
# --------------------------------------------------------------------------


def _render_pdf(title: str, subtitle: str, form_code: str, blocks: list[tuple]) -> bytes:
    """Render one static guideline PDF from a list of (kind, *args) blocks.

    Kinds:
      ("h2", str)                   — small-caps section heading
      ("p", str)                    — prose paragraph
      ("bullets", list[str])        — bullet list
      ("callout", title, body)      — coral tinted box
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    page_no = 1

    def _start_page() -> float:
        nonlocal page_no
        _draw_header(c, subtitle)
        _draw_footer(c, form_code, page_no, page_count=1)  # patched at end
        return PAGE_H - MARGIN - 60

    y = _start_page()
    y = _draw_h1(c, y, title)

    for kind, *args in blocks:
        if y < 120:
            c.showPage()
            page_no += 1
            y = _start_page()
        if kind == "h2":
            y = _draw_h2(c, y, args[0])
        elif kind == "p":
            y = _draw_prose(c, y, args[0])
        elif kind == "bullets":
            y = _draw_bullets(c, y, args[0])
        elif kind == "callout":
            y = _draw_callout(c, y, args[0], args[1])
        else:  # pragma: no cover
            raise ValueError(f"unknown block kind: {kind}")

    total_pages = page_no
    # Rewrite footer of every page with the correct total. Cheap for demo
    # sizes; reportlab doesn't expose a first-class placeholder.
    c.save()
    if total_pages == 1:
        return buf.getvalue()
    # If we spilled to multiple pages, redo the whole render with the
    # known page count. Only path that hits this is the appeals notice.
    buf2 = BytesIO()
    c2 = canvas.Canvas(buf2, pagesize=letter)
    page_no = 1

    def _start_page2() -> float:
        nonlocal page_no
        _draw_header(c2, subtitle)
        _draw_footer(c2, form_code, page_no, page_count=total_pages)
        return PAGE_H - MARGIN - 60

    y = _start_page2()
    y = _draw_h1(c2, y, title)
    for kind, *args in blocks:
        if y < 120:
            c2.showPage()
            page_no += 1
            y = _start_page2()
        if kind == "h2":
            y = _draw_h2(c2, y, args[0])
        elif kind == "p":
            y = _draw_prose(c2, y, args[0])
        elif kind == "bullets":
            y = _draw_bullets(c2, y, args[0])
        elif kind == "callout":
            y = _draw_callout(c2, y, args[0], args[1])
    c2.save()
    return buf2.getvalue()


# --------------------------------------------------------------------------
# The four guideline PDFs. Content is deliberately generic-but-plausible —
# reads like a real assistance-program enclosure without leaning on any
# real payer's brand voice.
# --------------------------------------------------------------------------


def _appeals_notice_pdf() -> bytes:
    return _render_pdf(
        title="Your appeal rights",
        subtitle="Enclosure to the CardioVita PAP denial notice",
        form_code="ASP-401",
        blocks=[
            ("p",
             "This notice explains what to do if you believe the enclosed decision on "
             "your CardioVita Patient Assistance Program application was wrong. Read it "
             "carefully — the appeal window is short and specific documents are required."),
            ("h2", "Deadline to appeal"),
            ("p",
             "You have sixty (60) calendar days from the date on the enclosed denial "
             "letter to file a first-level appeal. Appeals received after that date will "
             "not be reviewed unless you can show a good-cause reason for the delay."),
            ("h2", "How to file"),
            ("bullets", [
                "Write to Alderwood Appeals Coordinator, PO Box 42, Springfield, USA 00000.",
                "Include a copy of the enclosed denial letter (or the referral ID at the top of it).",
                "State clearly which part of the decision you disagree with and why.",
                "Attach any new evidence you'd like the reviewer to consider — updated income, "
                "clinical notes, prescriber statement, or proof of residency.",
            ]),
            ("h2", "What happens next"),
            ("p",
             "Your appeal will be reviewed by a coordinator who was not involved in the "
             "original decision. You will receive a written response within thirty (30) "
             "days of the date we receive your appeal. If we need more time we will "
             "notify you in writing before the deadline."),
            ("callout",
             "Need help filing?",
             "Call the Alderwood Patient Support line at 1-877-555-0100. Coordinators are "
             "available Monday–Friday 8am–8pm ET and can guide you through the appeal "
             "process at no cost."),
            ("h2", "External review"),
            ("p",
             "If your first-level appeal is denied, you may request an external review by "
             "an independent panel. Instructions and forms will be included with the "
             "first-level appeal response."),
        ],
    )


def _program_overview_pdf() -> bytes:
    return _render_pdf(
        title="Welcome to CardioVita PAP",
        subtitle="Program overview — please keep with your enrollment paperwork",
        form_code="ASP-301",
        blocks=[
            ("p",
             "Congratulations — your application to the CardioVita Patient Assistance "
             "Program has been approved. This overview summarizes how the program works, "
             "what to expect over the next twelve months, and how to reach us if anything "
             "changes."),
            ("h2", "How the program works"),
            ("bullets", [
                "Your approved medication is shipped to your home or your prescriber's office at no cost to you.",
                "Coverage runs for twelve (12) months from your approval date.",
                "You must remain enrolled with your current prescriber and pharmacy for the coverage window.",
                "You may need to re-enroll before the twelve months end — we'll contact you sixty (60) days before expiration.",
            ]),
            ("h2", "Your responsibilities"),
            ("p",
             "To keep your enrollment active, notify Alderwood within thirty (30) days if "
             "any of the following change: your address, your prescribing physician, your "
             "insurance status, or your household income."),
            ("callout",
             "Patient support",
             "Call 1-877-555-0100 for questions about shipments, refills, or your enrollment "
             "status. Have your referral ID ready — you'll find it at the top of your "
             "approval letter."),
        ],
    )


def _fulfillment_guide_pdf() -> bytes:
    return _render_pdf(
        title="Prescription fulfillment guide",
        subtitle="What to expect when your first shipment arrives",
        form_code="ASP-302",
        blocks=[
            ("h2", "Your first shipment"),
            ("p",
             "Your first shipment will arrive within ten (10) business days of the date "
             "on your approval letter. Shipments are sent by tracked overnight courier and "
             "require a signature from an adult at the delivery address."),
            ("h2", "Storage"),
            ("bullets", [
                "Refrigerate immediately upon delivery — 36°F to 46°F (2°C to 8°C).",
                "Do not freeze. Do not shake.",
                "Keep in the original carton until you are ready to use each dose.",
            ]),
            ("h2", "Refills"),
            ("p",
             "Refills are shipped automatically thirty (30) days before your current "
             "supply runs out. You do not need to call to reorder. If your dose or "
             "schedule changes, ask your prescriber to update the order with Alderwood."),
            ("callout",
             "Missing a shipment?",
             "If a shipment does not arrive within two business days of the expected date, "
             "call 1-877-555-0100. Do not skip doses — a coordinator will expedite a "
             "replacement at no cost."),
        ],
    )


def _missing_docs_checklist_pdf() -> bytes:
    return _render_pdf(
        title="What we still need",
        subtitle="Checklist — return with the missing documents",
        form_code="ASP-402",
        blocks=[
            ("p",
             "Your enclosed letter listed one or more documents we need to finish reviewing "
             "your CardioVita PAP application. Use this checklist as a guide when gathering "
             "the paperwork; you don't need to send anything not requested in the letter."),
            ("h2", "Commonly requested documents"),
            ("bullets", [
                "Proof of household income — last two pay stubs, most recent tax return, or a benefits statement.",
                "Proof of residency — a utility bill, lease agreement, or state ID with your current address.",
                "Insurance denial letter — the most recent one from your health plan, dated within the last twelve months.",
                "Prescriber statement — a signed note from your CardioVita prescriber confirming ongoing treatment.",
            ]),
            ("h2", "How to return the documents"),
            ("bullets", [
                "By mail: Alderwood PAP Intake, PO Box 41, Springfield, USA 00000.",
                "By fax: 1-877-555-0142.",
                "By secure upload: alderwoodrx.example/upload  (use the referral ID from your letter).",
            ]),
            ("callout",
             "Deadline",
             "Your application will remain open for forty-five (45) days from the letter date. "
             "If we don't receive the requested documents by then we'll close the file — you "
             "can reapply at any time."),
        ],
    )


STATIC_DOC_SPECS: list[dict] = [
    {
        "key": "alderwood-appeals-rights-notice",
        "name": "Appeals rights notice",
        "description": "Denial enclosure — how to appeal a CardioVita PAP decision.",
        "filename": "alderwood-appeals-rights-notice.pdf",
        "builder": _appeals_notice_pdf,
    },
    {
        "key": "alderwood-cardiovita-program-overview",
        "name": "CardioVita program overview",
        "description": "Approval enclosure — how the twelve-month PAP coverage works.",
        "filename": "alderwood-cardiovita-program-overview.pdf",
        "builder": _program_overview_pdf,
    },
    {
        "key": "alderwood-prescription-fulfillment-guide",
        "name": "Prescription fulfillment guide",
        "description": "Approval enclosure — first shipment, storage, and refills.",
        "filename": "alderwood-prescription-fulfillment-guide.pdf",
        "builder": _fulfillment_guide_pdf,
    },
    {
        "key": "alderwood-missing-documents-checklist",
        "name": "Missing documents checklist",
        "description": "Missing-info enclosure — how to return the documents we still need.",
        "filename": "alderwood-missing-documents-checklist.pdf",
        "builder": _missing_docs_checklist_pdf,
    },
]


PACKAGE_SPECS: list[dict] = [
    {
        "outcome": "approve",
        "name": "CardioVita approval bundle",
        "description": "Approval letter + program overview + fulfillment guide.",
        "template_hint": "approv",
        "static_keys": [
            "alderwood-cardiovita-program-overview",
            "alderwood-prescription-fulfillment-guide",
        ],
    },
    {
        "outcome": "deny",
        "name": "CardioVita denial bundle",
        "description": "Denial letter + appeals rights notice.",
        "template_hint": "deni",
        "static_keys": ["alderwood-appeals-rights-notice"],
    },
    {
        "outcome": "missing_info",
        "name": "CardioVita missing-info bundle",
        "description": "Missing-info letter + document checklist.",
        "template_hint": "missing",
        "static_keys": ["alderwood-missing-documents-checklist"],
    },
]


# --------------------------------------------------------------------------
# DB seeding
# --------------------------------------------------------------------------


async def _find_tenant(session: AsyncSession, name: str) -> Tenant:
    row = (
        await session.execute(select(Tenant).where(Tenant.name == name))
    ).scalar_one_or_none()
    if row is None:
        raise SystemExit(
            f"Tenant {name!r} not found. Create it via /ui/admin/tenants first, "
            "or override with ALDERWOOD_TENANT_NAME=<name>."
        )
    return row


async def _find_program(session: AsyncSession, tenant_id, slug: str) -> Program:
    row = (
        await session.execute(
            select(Program).where(Program.tenant_id == tenant_id, Program.slug == slug)
        )
    ).scalar_one_or_none()
    if row is None:
        raise SystemExit(
            f"Program {slug!r} not found in tenant. Create it via /ui/programs first, "
            "or override with ALDERWOOD_PROGRAM_SLUG=<slug>."
        )
    return row


async def _upsert_static_doc(
    session: AsyncSession, tenant_id, spec: dict
) -> StaticDocument:
    existing = (
        await session.execute(
            select(StaticDocument).where(
                StaticDocument.tenant_id == tenant_id,
                StaticDocument.name == spec["name"],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (existing.current_version or 0) > 0:
            print(f"  - static doc {spec['name']!r} already has v{existing.current_version}, skipping upload")
            return existing
        doc = existing
    else:
        doc = await create_static_document(
            session, tenant_id, name=spec["name"], description=spec["description"]
        )
        print(f"  + created static doc {spec['name']!r}")

    body = spec["builder"]()
    await add_static_document_version(session, doc, body=body, filename=spec["filename"])
    print(f"    uploaded v{doc.current_version} ({len(body)} bytes, "
          f"blob=static-docs/{tenant_id}/{doc.id}/v{doc.current_version}/{spec['filename']})")
    return doc


def _find_reply_template_sync(
    session: AsyncSession, tenant_id, hint: str
):
    """Async wrapper needed — reuse the same latest-frozen resolution the
    assembler uses so the seeded package points at the operator-selected
    reply template."""
    raise RuntimeError  # keep type checkers happy; never called (async version below)


async def _find_reply_template(
    session: AsyncSession, tenant_id, hint: str
) -> Template | None:
    stmt = (
        select(Template)
        .where(
            Template.tenant_id == tenant_id,
            Template.is_active.is_(True),
            Template.name.ilike(f"%{hint}%"),
        )
        .order_by(Template.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _upsert_doc_package(
    session: AsyncSession, tenant_id, spec: dict, doc_by_key: dict
) -> DocPackage | None:
    tmpl = await _find_reply_template(session, tenant_id, spec["template_hint"])
    if tmpl is None:
        print(f"  ! no reply template matched hint {spec['template_hint']!r} — "
              f"skipping package {spec['name']!r}")
        return None

    items = [{"kind": "template", "ref_id": str(tmpl.id), "label": f"Reply letter — {spec['outcome']}"}]
    for key in spec["static_keys"]:
        doc = doc_by_key.get(key)
        if doc is None:
            print(f"  ! missing static doc key {key!r} — skipping in package {spec['name']!r}")
            continue
        items.append({"kind": "static", "ref_id": str(doc.id), "label": doc.name})

    existing = (
        await session.execute(
            select(DocPackage).where(
                DocPackage.tenant_id == tenant_id,
                DocPackage.name == spec["name"],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.items = normalize_package_items(items)
        existing.description = spec["description"]
        existing.is_active = True
        await session.flush()
        print(f"  - package {spec['name']!r} refreshed ({len(items)} items)")
        return existing

    pkg = await create_doc_package(
        session, tenant_id, name=spec["name"],
        description=spec["description"], items=items,
    )
    print(f"  + created package {spec['name']!r} ({len(items)} items)")
    return pkg


async def _bind_program_outcomes(
    session: AsyncSession, program: Program, packages_by_outcome: dict[str, DocPackage]
) -> None:
    outcomes = ["approve", "deny", "missing_info"]
    program.outcomes = outcomes
    bindings = {
        outcome: str(pkg.id) for outcome, pkg in packages_by_outcome.items()
    }
    program.package_bindings = bindings
    await session.flush()
    print(f"  - bound {len(bindings)} outcomes on program {program.slug!r}:")
    for outcome, pkg_id in bindings.items():
        pkg = packages_by_outcome[outcome]
        print(f"      {outcome:14s} >> {pkg.name}")


async def main() -> None:
    tenant_name = os.environ.get("ALDERWOOD_TENANT_NAME", "Alderwood Specialty Pharmacy")
    program_slug = os.environ.get("ALDERWOOD_PROGRAM_SLUG", "cardiovita-pap-2026")

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        async with session.begin():
            print(f">> resolving tenant {tenant_name!r} and program {program_slug!r}")
            tenant = await _find_tenant(session, tenant_name)
            program = await _find_program(session, tenant.id, program_slug)
            print(f"  tenant_id={tenant.id}  program_id={program.id}")

            print(">> upserting static guideline PDFs")
            doc_by_key: dict = {}
            for spec in STATIC_DOC_SPECS:
                doc = await _upsert_static_doc(session, tenant.id, spec)
                doc_by_key[spec["key"]] = doc

            print(">> upserting doc packages")
            packages_by_outcome: dict = {}
            for spec in PACKAGE_SPECS:
                pkg = await _upsert_doc_package(session, tenant.id, spec, doc_by_key)
                if pkg is not None:
                    packages_by_outcome[spec["outcome"]] = pkg

            if not packages_by_outcome:
                raise SystemExit(
                    "No packages created — no reply templates matched the outcome hints. "
                    "Upload the Alderwood approval/denial/missing-info templates first."
                )

            print(">> wiring program bindings")
            await _bind_program_outcomes(session, program, packages_by_outcome)

    await engine.dispose()
    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
