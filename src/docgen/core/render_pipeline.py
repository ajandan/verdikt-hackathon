"""Shared render primitives used by every entry point (REST, UI, MCP, worker).

Extracted to avoid four near-identical copies of the merge → optional-PDF
→ blob-write incantation, and to give `FORMAT_META` a single source of truth.

These functions are intentionally pure I/O helpers — they don't touch the
database. Each call site keeps its own DB and error-handling contract
(HTTPException vs ValueError vs HTML flash vs actor state flip).
"""
from __future__ import annotations

import uuid

from docgen.config import Settings
from docgen.core.merge import merge_docx
from docgen.core.pdf import docx_to_pdf
from docgen.core.render_pipeline_pdf import execute_pdf_render, persist_pdf_result
from docgen.core.schema_infer import coerce_payload
from docgen.store.blob import get_object, put_object
from docgen.store.models import TemplateVersion

FORMAT_META: dict[str, tuple[str, str]] = {
    "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pdf": ("pdf", "application/pdf"),
}


def format_meta(output_format: str) -> tuple[str, str]:
    """(ext, content_type) for an output format, falling back to docx."""
    return FORMAT_META.get(output_format, FORMAT_META["docx"])


def fetch_template_bytes(tv: TemplateVersion, *, settings: Settings) -> bytes:
    """Pull the source .docx blob for a frozen version."""
    return get_object(settings.s3_bucket_templates, tv.blob_key)


def merge_and_maybe_pdf(
    template_bytes: bytes,
    tv: TemplateVersion,
    data: dict,
    output_format: str,
    *,
    settings: Settings,
) -> bytes:
    """Run docxtpl merge, then convert to PDF if the caller asked for one."""
    output = merge_docx(template_bytes, coerce_payload(tv.schema, data))
    if output_format == "pdf":
        output = docx_to_pdf(
            output, timeout_seconds=settings.pdf_conversion_timeout_seconds
        )
    return output


def execute_merge(
    tv: TemplateVersion,
    data: dict,
    output_format: str,
    *,
    settings: Settings,
) -> bytes:
    """Fetch the template blob, run merge, optionally convert to PDF.

    Dispatches by template kind. PDF templates are detected by pdf_flavor
    being set — kind='pdf' rows always have it, and docx rows never do.
    Using the column directly avoids an extra JOIN back to Template just to
    read a discriminator.
    """
    if tv.pdf_flavor is not None:
        return execute_pdf_render(tv, data, output_format, settings=settings)
    return merge_and_maybe_pdf(
        fetch_template_bytes(tv, settings=settings),
        tv, data, output_format, settings=settings,
    )


def persist_result(
    tv: TemplateVersion,
    job_id: uuid.UUID,
    output: bytes,
    output_format: str,
    *,
    settings: Settings,
) -> str:
    """Upload rendered bytes to the documents bucket. Returns the blob key."""
    if tv.pdf_flavor is not None:
        return persist_pdf_result(tv, job_id, output, settings=settings)
    ext, ctype = format_meta(output_format)
    result_key = f"renders/{tv.template_id}/v{tv.version}/{job_id}.{ext}"
    put_object(settings.s3_bucket_documents, result_key, output, content_type=ctype)
    return result_key
