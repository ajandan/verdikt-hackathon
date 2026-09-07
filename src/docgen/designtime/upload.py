"""Design-time step 1–2 (§6): accept .docx or .pdf, preflight, persist draft."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.config import get_settings
from docgen.core.docx_sanitize import sanitize_docx
from docgen.core.preflight import PreflightReport, preflight
from docgen.designtime.audit import record_audit_event
from docgen.designtime.preflight_pdf import PdfPreflightReport, preflight_pdf
from docgen.designtime.realign_pdf import RealignReport, realign_placements
from docgen.store.blob import put_object
from docgen.store.models import Template, TemplateVersion

log = structlog.get_logger(__name__)


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pdf_field_type_to_schema_type(pdf_field_type: str | None) -> str:
    # preflight_pdf collapses /FT into: "text", "checkbox" (incl. radio/pushbutton),
    # "choice", or None. Only checkboxes map to a distinct schema type today —
    # choices would need /Opt harvesting to become an enum, out of scope here.
    if pdf_field_type == "checkbox":
        return "boolean"
    return "string"


async def upload_template(
    session: AsyncSession,
    *,
    name: str,
    docx_bytes: bytes,
    tenant_id: uuid.UUID,
    owner: str | None = None,
    template_id: uuid.UUID | None = None,
) -> tuple[Template, TemplateVersion, PreflightReport]:
    # Upload-time hardening runs before preflight so a hostile / malformed
    # payload never reaches python-docx or blob storage. Macros get stripped
    # rather than rejected: a legitimate operator may have uploaded a .docm
    # renamed to .docx, and we'd rather keep the body of the template than
    # bounce them with a confusing error.
    docx_bytes, sanitize_report = sanitize_docx(docx_bytes)
    report = preflight(docx_bytes)

    if template_id is not None:
        template = await session.get(Template, template_id)
        if template is None:
            raise ValueError(f"Template {template_id} does not exist")
        if template.tenant_id != tenant_id:
            raise ValueError(f"Template {template_id} does not exist")
    else:
        template = Template(name=name, owner=owner, tenant_id=tenant_id)
        session.add(template)
        await session.flush()

    latest = await session.execute(
        select(TemplateVersion.version)
        .where(TemplateVersion.template_id == template.id)
        .order_by(TemplateVersion.version.desc())
        .limit(1)
    )
    prev = latest.scalar_one_or_none() or 0
    version = prev + 1

    settings = get_settings()
    blob_key = f"draft/{template.id}/v{version}/{_content_hash(docx_bytes)[:12]}.docx"
    put_object(
        settings.s3_bucket_templates,
        blob_key,
        docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    tv = TemplateVersion(
        template_id=template.id,
        version=version,
        locale="default",
        schema={"fields": []},  # populated on freeze
        preflight=report.to_dict(),
        blob_key=blob_key,
        version_hash="draft-" + _content_hash(docx_bytes)[:16],
        status="draft" if report.outcome != "reject" else "rejected",
        is_frozen=False,
        created_by=owner,
    )
    session.add(tv)
    await session.flush()
    audit_summary: dict[str, Any] = {"kind": "docx", "outcome": report.outcome}
    if sanitize_report.macros_stripped:
        audit_summary["macros_stripped"] = sanitize_report.macros_stripped
    await record_audit_event(
        session,
        template_id=template.id,
        version_id=tv.id,
        actor=owner,
        event_kind="version_created",
        summary=audit_summary,
    )
    await session.commit()
    await session.refresh(template)
    await session.refresh(tv)

    log.info(
        "template_uploaded",
        template_id=str(template.id),
        tenant_id=str(tenant_id),
        version_id=str(tv.id),
        version=tv.version,
        outcome=report.outcome,
        blob_key=blob_key,
        bytes=len(docx_bytes),
        owner=owner,
    )
    _ = datetime.now(UTC)  # placeholder for later audit hook
    return template, tv, report


async def upload_locale_variant(
    session: AsyncSession,
    *,
    template_id: uuid.UUID,
    tenant_id: uuid.UUID,
    version_number: int,
    locale: str,
    docx_bytes: bytes,
    owner: str | None = None,
) -> tuple[TemplateVersion, PreflightReport]:
    """Attach a localized draft to an existing version_number.

    Unlike upload_template, this does NOT increment version. The resulting row
    shares (template_id, version_number) with sibling locales; freeze-time
    invariant then guarantees they all advertise the same schema fields.
    """
    if not locale or locale == "default":
        raise ValueError("locale must be a non-default tag (e.g. 'en-GB', 'es-419')")

    template = await session.get(Template, template_id)
    if template is None or template.tenant_id != tenant_id:
        raise ValueError(f"Template {template_id} does not exist")

    existing = await session.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version == version_number,
        )
    )
    siblings = existing.scalars().all()
    if not siblings:
        raise ValueError(f"Version {version_number} does not exist for this template")
    if any(s.locale == locale for s in siblings):
        raise ValueError(f"Locale '{locale}' already exists for v{version_number}")

    docx_bytes, sanitize_report = sanitize_docx(docx_bytes)
    report = preflight(docx_bytes)

    settings = get_settings()
    blob_key = f"draft/{template_id}/v{version_number}/{locale}-{_content_hash(docx_bytes)[:12]}.docx"
    put_object(
        settings.s3_bucket_templates,
        blob_key,
        docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    tv = TemplateVersion(
        template_id=template_id,
        version=version_number,
        locale=locale,
        schema={"fields": []},
        preflight=report.to_dict(),
        blob_key=blob_key,
        version_hash="draft-" + _content_hash(docx_bytes)[:16],
        status="draft" if report.outcome != "reject" else "rejected",
        is_frozen=False,
        created_by=owner,
    )
    session.add(tv)
    await session.flush()
    locale_summary: dict[str, Any] = {"locale": locale, "outcome": report.outcome}
    if sanitize_report.macros_stripped:
        locale_summary["macros_stripped"] = sanitize_report.macros_stripped
    await record_audit_event(
        session,
        template_id=template_id,
        version_id=tv.id,
        actor=owner,
        event_kind="locale_variant_created",
        summary=locale_summary,
    )
    await session.commit()
    await session.refresh(tv)

    log.info(
        "locale_variant_uploaded",
        template_id=str(template_id),
        tenant_id=str(tenant_id),
        version_id=str(tv.id),
        version=version_number,
        locale=locale,
        outcome=report.outcome,
        blob_key=blob_key,
        bytes=len(docx_bytes),
    )
    return tv, report


async def upload_pdf_template(
    session: AsyncSession,
    *,
    name: str,
    pdf_bytes: bytes,
    tenant_id: uuid.UUID,
    owner: str | None = None,
    template_id: uuid.UUID | None = None,
) -> tuple[Template, TemplateVersion, PdfPreflightReport]:
    """PDF sibling of upload_template.

    Kind is fixed to 'pdf' on the Template row. Flavor ('acroform' | 'flat')
    is decided by preflight based on whether the source has widgets:
      - acroform: initial placements are auto-harvested from widget rects,
                  so the template is renderable immediately.
      - flat:     placements starts empty; the user must run the PR2 editor
                  before the template will produce any output.

    Adding a new version to an existing template must not change kind — a
    docx template stays docx, a pdf template stays pdf. Enforced here.
    """
    report = preflight_pdf(pdf_bytes)

    if template_id is not None:
        template = await session.get(Template, template_id)
        if template is None or template.tenant_id != tenant_id:
            raise ValueError(f"Template {template_id} does not exist")
        if template.kind != "pdf":
            raise ValueError(
                f"Template {template_id} is a {template.kind} template — "
                "cannot upload a PDF version to it"
            )
    else:
        template = Template(name=name, owner=owner, tenant_id=tenant_id, kind="pdf")
        session.add(template)
        await session.flush()

    # Grab the previous version's placements BEFORE we know the new version
    # number — used for realign below.
    prior_placements: list[dict] = []
    prior_tv = await session.execute(
        select(TemplateVersion)
        .where(TemplateVersion.template_id == template.id)
        .order_by(TemplateVersion.version.desc(), TemplateVersion.subversion.desc())
        .limit(1)
    )
    prior_row = prior_tv.scalar_one_or_none()
    if prior_row is not None and prior_row.pdf_field_placements:
        prior_placements = list(
            prior_row.pdf_field_placements.get("placements") or []
        )
    prev_version_number = prior_row.version if prior_row is not None else 0
    version = prev_version_number + 1

    settings = get_settings()
    blob_key = f"draft/{template.id}/v{version}/{_content_hash(pdf_bytes)[:12]}.pdf"
    put_object(
        settings.s3_bucket_templates,
        blob_key,
        pdf_bytes,
        content_type="application/pdf",
    )

    # Realign prior placements against the fresh preflight. Skip on the
    # first upload (nothing to align) and on a preflight-reject (the new
    # PDF isn't renderable so there's nothing sensible to align onto).
    # Realigned placements REPLACE the raw preflight output — the new-widget
    # branch inside realign_placements picks up any newly-added acroform
    # widgets so nothing is silently dropped.
    realign_report: RealignReport | None = None
    if prior_placements and report.outcome != "reject":
        placements, realign_report = realign_placements(
            prior_placements=prior_placements,
            new_placements=report.initial_placements,
            new_page_count=report.page_count,
        )
    else:
        placements = list(report.initial_placements)

    # Schema for a PDF template is derived from the aligned placements:
    # checkboxes/radios become boolean, everything else is string. Flat
    # templates on first upload land here with no placements — schema is
    # empty until the user places fields in the editor.
    schema_fields = [
        {"name": p["name"], "type": _pdf_field_type_to_schema_type(p.get("field_type"))}
        for p in placements
    ]

    pdf_field_placements: dict[str, Any] = {"placements": placements}
    if realign_report is not None:
        # Stashed alongside placements so the editor + audit trail can
        # surface the realign summary without a second store lookup.
        pdf_field_placements["realign"] = realign_report.to_dict()

    tv = TemplateVersion(
        template_id=template.id,
        version=version,
        locale="default",
        schema={"fields": schema_fields},
        preflight=report.to_dict(),
        blob_key=blob_key,
        version_hash="draft-" + _content_hash(pdf_bytes)[:16],
        # Flat PDFs land in draft with no placements — they can't render yet,
        # but they're not rejected either. The editor (PR2) turns them
        # renderable. Only preflight-reject outcomes flip to 'rejected'.
        status="rejected" if report.outcome == "reject" else "draft",
        is_frozen=False,
        pdf_flavor=report.flavor,
        pdf_field_placements=pdf_field_placements,
        created_by=owner,
    )
    session.add(tv)
    await session.flush()
    await record_audit_event(
        session,
        template_id=template.id,
        version_id=tv.id,
        actor=owner,
        event_kind="version_created",
        summary={
            "kind": "pdf",
            "flavor": report.flavor,
            "outcome": report.outcome,
            "placements": len(placements),
            "realign": realign_report.to_dict() if realign_report else None,
        },
    )
    await session.commit()
    await session.refresh(template)
    await session.refresh(tv)

    log.info(
        "pdf_template_uploaded",
        template_id=str(template.id),
        tenant_id=str(tenant_id),
        version_id=str(tv.id),
        version=tv.version,
        flavor=report.flavor,
        page_count=report.page_count,
        placements=len(placements),
        outcome=report.outcome,
        blob_key=blob_key,
        bytes=len(pdf_bytes),
        owner=owner,
        realign=realign_report.to_dict() if realign_report else None,
    )
    return template, tv, report
