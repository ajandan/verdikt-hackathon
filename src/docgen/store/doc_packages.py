"""Storage helpers for the doc-package model.

Thin functions over the SQLAlchemy models — no business logic. Kept in one
module because the admin routes, the assembler stage, and the tests all
share the same primitives (hash, upload blob, add version, list versions,
resolve latest).

See ADR-0003 for the model and the version-pinning rationale.
"""
from __future__ import annotations

import hashlib
import io
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.config import get_settings
from docgen.store.blob import get_object, put_object
from docgen.store.models import (
    DocPackage,
    StaticDocument,
    StaticDocumentVersion,
)


_PDF_MAGIC = b"%PDF-"


class StaticDocRejected(Exception):
    """Raised by `add_static_document_version` when the payload fails the
    lightweight pre-store checks. Handlers translate this to a user-facing
    flash / HTTP 400 in the same shape as the DOCX/PDF preflight rejects."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _reject_if_not_pdf(body: bytes) -> None:
    if len(body) < 5 or not body.startswith(_PDF_MAGIC):
        raise StaticDocRejected(
            "static.not_pdf",
            "Only PDF files are accepted in the static document library.",
        )


def _page_count(body: bytes) -> int | None:
    """Best-effort page probe. Returns None if pypdfium2 can't open the file
    — the caller still stores it (the assembler will fail loud if it truly
    can't be merged, which is caught upstream by the pipeline preflight)."""
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover — pypdfium2 is a hard dep in prod
        return None
    try:
        pdf = pdfium.PdfDocument(io.BytesIO(body))
        n = len(pdf)
        pdf.close()
        return int(n)
    except Exception:
        return None


def _blob_key(tenant_id: uuid.UUID, doc_id: uuid.UUID, version: int, filename: str) -> str:
    """Deterministic blob key so a `resolved_items` row is enough to fetch
    the exact bytes even if the DB row is later archived."""
    safe = filename.strip().replace("/", "_") or "document.pdf"
    return f"static-docs/{tenant_id}/{doc_id}/v{version}/{safe}"


async def create_static_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    description: str | None = None,
) -> StaticDocument:
    """Create the parent row. Versions get added via `add_static_document_version`."""
    doc = StaticDocument(
        tenant_id=tenant_id,
        name=name.strip(),
        description=(description or None),
        current_version=0,
    )
    session.add(doc)
    await session.flush()
    return doc


async def add_static_document_version(
    session: AsyncSession,
    doc: StaticDocument,
    body: bytes,
    filename: str,
    uploaded_by: uuid.UUID | None = None,
) -> StaticDocumentVersion:
    """Upload bytes to blob store + append a version row. Bumps current_version.

    Raises `StaticDocRejected` for non-PDF payloads. Encrypted / corrupt PDFs
    still store — the assembler catches those at merge time. The pre-store
    check is deliberately narrow.
    """
    _reject_if_not_pdf(body)

    settings = get_settings()
    next_version = int(doc.current_version or 0) + 1
    key = _blob_key(doc.tenant_id, doc.id, next_version, filename)
    put_object(settings.s3_bucket_documents, key, body, content_type="application/pdf")

    ver = StaticDocumentVersion(
        static_document_id=doc.id,
        version=next_version,
        blob_key=key,
        content_hash=hashlib.sha256(body).hexdigest(),
        byte_size=len(body),
        page_count=_page_count(body),
        uploaded_by=uploaded_by,
    )
    session.add(ver)
    doc.current_version = next_version
    await session.flush()
    return ver


async def list_static_document_versions(
    session: AsyncSession, doc_id: uuid.UUID
) -> list[StaticDocumentVersion]:
    stmt = (
        select(StaticDocumentVersion)
        .where(StaticDocumentVersion.static_document_id == doc_id)
        .order_by(StaticDocumentVersion.version.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def latest_static_document_version(
    session: AsyncSession, doc_id: uuid.UUID
) -> StaticDocumentVersion | None:
    """Resolve the version the assembler will use for this static doc — always
    the highest version number. No pin, no draft state; the latest upload is
    the live one. See ADR-0003 for the auto-follow rationale."""
    stmt = (
        select(StaticDocumentVersion)
        .where(StaticDocumentVersion.static_document_id == doc_id)
        .order_by(StaticDocumentVersion.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def load_static_document_bytes(version: StaticDocumentVersion) -> bytes:
    """Fetch the raw PDF bytes for an assembly step."""
    settings = get_settings()
    return get_object(settings.s3_bucket_documents, version.blob_key)


# ------------------------------------------------------------------------
# Doc packages
# ------------------------------------------------------------------------


_VALID_ITEM_KINDS = frozenset({"template", "static"})


class DocPackageValidationError(Exception):
    """Raised by `normalize_package_items` when the operator-submitted list
    doesn't match the shape `{kind, ref_id, label?}`."""


def normalize_package_items(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize the operator's item list before persisting.

    - Trims to `{kind, ref_id, label}` — anything else is dropped so the JSONB
      column doesn't accumulate stray keys.
    - Enforces `kind in {"template", "static"}`.
    - Enforces `ref_id` is a valid UUID string.
    - Preserves order.
    """
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DocPackageValidationError(f"Item {i}: not an object")
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in _VALID_ITEM_KINDS:
            raise DocPackageValidationError(
                f"Item {i}: kind must be 'template' or 'static' (got {kind!r})"
            )
        try:
            ref_id = str(uuid.UUID(str(item.get("ref_id", ""))))
        except (ValueError, TypeError) as e:
            raise DocPackageValidationError(f"Item {i}: ref_id is not a valid UUID") from e
        label = item.get("label")
        entry: dict[str, Any] = {"kind": kind, "ref_id": ref_id}
        if isinstance(label, str) and label.strip():
            entry["label"] = label.strip()
        out.append(entry)
    return out


async def create_doc_package(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    description: str | None = None,
    items: list[dict[str, Any]] | None = None,
) -> DocPackage:
    pkg = DocPackage(
        tenant_id=tenant_id,
        name=name.strip(),
        description=(description or None),
        items=normalize_package_items(items or []),
    )
    session.add(pkg)
    await session.flush()
    return pkg
