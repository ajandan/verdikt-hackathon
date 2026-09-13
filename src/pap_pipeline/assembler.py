"""Assemble a program's outcome-bound doc package into one merged PDF.

The pipeline's fourth stage (extract → retrieve → reason → **assemble**).
Resolves `program.package_bindings[decision.outcome]` to a DocPackage, then
walks its ordered items:

  - kind='template': latest frozen TemplateVersion for the referenced
    Template, rendered to PDF via `docgen.core.render_pipeline.execute_merge`
    (the same call the single-template reply flow uses).
  - kind='static': latest StaticDocumentVersion for the referenced
    StaticDocument, bytes streamed from the documents blob store as-is.

Bytes are concatenated in declared order with pypdf, uploaded to the
documents bucket, and a `doc_package_assemblies` provenance row is written
that stamps the exact (template_version_id, static_document_version_id)
tuple per item — so "which bytes did this case receive?" is one PK lookup.

See ADR-0003 for the auto-follow version-pinning decision and the
missing-binding failure mode.
"""
from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from typing import Any

import pypdf
import structlog

from docgen.config import Settings
from docgen.core.render_pipeline import execute_merge
from docgen.store.blob import get_object, put_object
from docgen.store.models import (
    DocPackage,
    DocPackageAssembly,
    InboxEvent,
    Program,
    StaticDocument,
    StaticDocumentVersion,
    Template,
    TemplateVersion,
    Tenant,
)
from pap_pipeline.schemas.decision import Decision

log = structlog.get_logger(__name__)


class AssemblerError(Exception):
    """Base class so callers can catch every assembler failure with one
    except block. Subclasses distinguish the three ways assembly can fail
    (no binding, missing referenced row, render/merge blew up)."""


class AssemblerMissingBinding(AssemblerError):
    """The program has no package bound to this decision outcome.

    Raised as a hard error — a program that's been published without a
    binding for every declared outcome is an operator misconfiguration,
    not a pipeline bug. The caller decides whether to fall back to the
    legacy single-template flow (during migration) or surface the failure.
    """


class AssemblerItemMissing(AssemblerError):
    """A package item references a Template or StaticDocument that's gone,
    or has no published version yet. Same shape as the composer's "missing"
    warning — visible in the UI but only fatal at assembly time."""


class AssemblerRenderFailed(AssemblerError):
    """A per-item render or PDF merge blew up. Attaches the failing item
    label so the operator can jump straight to the offender."""


@dataclass(frozen=True)
class AssemblyResult:
    """What the assembler hands back to the caller.

    `blob_key` is the deterministic path the merged PDF was written to;
    `assembly_id` points at the append-only provenance row so downstream
    systems can join back to `resolved_items` for the per-item versions.
    """

    pdf_bytes: bytes
    filename: str
    blob_key: str
    assembly_id: uuid.UUID
    page_count: int | None
    byte_size: int
    item_count: int


def assemble_reply_package(
    session: Any,
    *,
    tenant: Tenant,
    program: Program,
    decision: Decision,
    event: InboxEvent,
    render_data: dict[str, Any],
    settings: Settings,
) -> AssemblyResult:
    """Resolve → render → merge → persist. Sync entrypoint for the inbox hook.

    `render_data` is the pre-built merge context (the same dict the legacy
    `_render_reply_attachment` computes) — the assembler doesn't know how to
    build it because that logic depends on the reply-letter schema, which
    lives in the docgen side of the fence.
    """
    outcome = decision.outcome
    bindings = dict(program.package_bindings or {})
    pkg_id_str = bindings.get(outcome)
    if not pkg_id_str:
        raise AssemblerMissingBinding(
            f"program {program.slug!r} has no package bound to outcome {outcome!r}"
        )
    try:
        pkg_id = uuid.UUID(pkg_id_str)
    except (TypeError, ValueError) as e:
        raise AssemblerMissingBinding(
            f"program {program.slug!r} binding for {outcome!r} is not a valid uuid"
        ) from e

    pkg = session.get(DocPackage, pkg_id)
    if pkg is None or pkg.tenant_id != tenant.id:
        raise AssemblerMissingBinding(
            f"package {pkg_id} bound to outcome {outcome!r} not found in tenant"
        )
    items = list(pkg.items or [])
    if not items:
        raise AssemblerMissingBinding(
            f"package {pkg.name!r} has no items — nothing to assemble"
        )

    resolved: list[dict[str, Any]] = []
    part_bytes: list[bytes] = []

    for pos, item in enumerate(items):
        kind = item.get("kind")
        ref_id_raw = item.get("ref_id")
        label = item.get("label") or ""
        try:
            ref_id = uuid.UUID(ref_id_raw)
        except (TypeError, ValueError) as e:
            raise AssemblerItemMissing(
                f"item #{pos + 1} in package {pkg.name!r} has invalid ref_id"
            ) from e

        if kind == "template":
            body, meta = _render_template_item(session, tenant, ref_id, render_data, settings, pos, label)
        elif kind == "static":
            body, meta = _load_static_item(session, tenant, ref_id, pos, label)
        else:
            raise AssemblerItemMissing(
                f"item #{pos + 1} in package {pkg.name!r} has unknown kind {kind!r}"
            )
        part_bytes.append(body)
        resolved.append(meta)

    merged, page_count = _merge_pdfs(part_bytes, package_name=pkg.name)
    filename = f"package_{outcome}_{_short_id(event.id)}.pdf"
    blob_key = f"doc-packages/{tenant.id}/{event.id}/{filename}"
    try:
        put_object(
            settings.s3_bucket_documents,
            blob_key,
            merged,
            content_type="application/pdf",
        )
    except Exception as e:  # pragma: no cover — bucket errors are logged upstream
        raise AssemblerRenderFailed(
            f"failed to persist merged package to blob store: {type(e).__name__}: {e}"
        ) from e

    assembly = DocPackageAssembly(
        tenant_id=tenant.id,
        doc_package_id=pkg.id,
        inbox_event_id=event.id,
        outcome=outcome,
        resolved_items=resolved,
        merged_blob_key=blob_key,
        merged_byte_size=len(merged),
        merged_page_count=page_count,
    )
    session.add(assembly)
    session.flush()

    log.info(
        "pap_package_assembled",
        event_id=str(event.id),
        package_id=str(pkg.id),
        outcome=outcome,
        item_count=len(items),
        byte_size=len(merged),
        page_count=page_count,
        assembly_id=str(assembly.id),
    )

    return AssemblyResult(
        pdf_bytes=merged,
        filename=filename,
        blob_key=blob_key,
        assembly_id=assembly.id,
        page_count=page_count,
        byte_size=len(merged),
        item_count=len(items),
    )


def _render_template_item(
    session: Any,
    tenant: Tenant,
    template_id: uuid.UUID,
    render_data: dict[str, Any],
    settings: Settings,
    pos: int,
    label: str,
) -> tuple[bytes, dict[str, Any]]:
    """Render one template item to PDF bytes and stamp its provenance row.

    Latest-frozen-version resolution mirrors `_find_reply_template` — no pin,
    the assembler always picks the highest (version, subversion). If nothing
    is frozen yet the item is treated as missing so the operator gets a
    surgical error message.
    """
    tmpl = session.get(Template, template_id)
    if tmpl is None or tmpl.tenant_id != tenant.id or not tmpl.is_active:
        raise AssemblerItemMissing(
            f"item #{pos + 1} ({label or 'template'}) references template "
            f"{template_id} which is missing or inactive"
        )
    tv = (
        session.query(TemplateVersion)
        .filter(
            TemplateVersion.template_id == tmpl.id,
            TemplateVersion.is_frozen.is_(True),
        )
        .order_by(
            TemplateVersion.version.desc(),
            TemplateVersion.subversion.desc(),
            TemplateVersion.created_at.desc(),
        )
        .first()
    )
    if tv is None:
        raise AssemblerItemMissing(
            f"item #{pos + 1} ({label or tmpl.name}) has no frozen version yet"
        )
    try:
        pdf_bytes = execute_merge(tv, render_data, output_format="pdf", settings=settings)
    except Exception as e:
        raise AssemblerRenderFailed(
            f"item #{pos + 1} ({label or tmpl.name}) render failed: "
            f"{type(e).__name__}: {e}"
        ) from e
    meta = {
        "position": pos,
        "kind": "template",
        "label": label,
        "template_id": str(tmpl.id),
        "template_name": tmpl.name,
        "template_version_id": str(tv.id),
        "template_version": f"{tv.version}.{tv.subversion}",
    }
    return pdf_bytes, meta


def _load_static_item(
    session: Any,
    tenant: Tenant,
    doc_id: uuid.UUID,
    pos: int,
    label: str,
) -> tuple[bytes, dict[str, Any]]:
    """Fetch the latest StaticDocumentVersion bytes and stamp its provenance row."""
    doc = session.get(StaticDocument, doc_id)
    if doc is None or doc.tenant_id != tenant.id or not doc.is_active:
        raise AssemblerItemMissing(
            f"item #{pos + 1} ({label or 'static doc'}) references static document "
            f"{doc_id} which is missing or inactive"
        )
    version = (
        session.query(StaticDocumentVersion)
        .filter(StaticDocumentVersion.static_document_id == doc.id)
        .order_by(StaticDocumentVersion.version.desc())
        .first()
    )
    if version is None:
        raise AssemblerItemMissing(
            f"item #{pos + 1} ({label or doc.name}) has no version uploaded yet"
        )
    from docgen.config import get_settings
    try:
        body = get_object(get_settings().s3_bucket_documents, version.blob_key)
    except Exception as e:
        raise AssemblerRenderFailed(
            f"item #{pos + 1} ({label or doc.name}) blob fetch failed: "
            f"{type(e).__name__}: {e}"
        ) from e
    meta = {
        "position": pos,
        "kind": "static",
        "label": label,
        "static_document_id": str(doc.id),
        "static_document_name": doc.name,
        "static_document_version_id": str(version.id),
        "static_document_version": version.version,
        "content_hash": version.content_hash,
    }
    return body, meta


def _merge_pdfs(parts: list[bytes], *, package_name: str) -> tuple[bytes, int | None]:
    """Concatenate PDF byte blobs into one document. Preserves page order."""
    writer = pypdf.PdfWriter()
    total_pages = 0
    for i, blob in enumerate(parts):
        try:
            reader = pypdf.PdfReader(io.BytesIO(blob))
        except Exception as e:
            raise AssemblerRenderFailed(
                f"package {package_name!r} item #{i + 1} is not a valid PDF: "
                f"{type(e).__name__}: {e}"
            ) from e
        for page in reader.pages:
            writer.add_page(page)
            total_pages += 1
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), (total_pages or None)


def _short_id(event_id: Any) -> str:
    return str(event_id).replace("-", "")[:8].upper()


__all__ = [
    "AssemblerError",
    "AssemblerMissingBinding",
    "AssemblerItemMissing",
    "AssemblerRenderFailed",
    "AssemblyResult",
    "assemble_reply_package",
]
