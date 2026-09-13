"""Token-gated downloads for artifacts referenced from outbound emails.

Kept outside the /ui/* and /api/* routers on purpose: /ui/* is guarded by
the admin session middleware (would redirect email recipients to /ui/login)
and /api/* by Bearer auth (email recipients have no key). This module
carries its own routes that ride solely on the signed token in the URL.
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from docgen.auth import verify_download_token, verify_inbox_test_token
from docgen.config import get_settings
from docgen.store.blob import get_object
from docgen.store.db import AsyncSessionLocal
from docgen.store.models import InboxEvent, InboxTestResult

log = structlog.get_logger(__name__)

router = APIRouter(tags=["downloads"], include_in_schema=False)


def _content_type(ext: str | None) -> str:
    if ext == "pdf":
        return "application/pdf"
    return (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    )


@router.get("/downloads/inbox-test/{test_result_id}")
async def download_inbox_test_artifact(
    test_result_id: uuid.UUID,
    token: str = Query(..., description="Signed download token"),
) -> Response:
    """Stream the rendered artifact for one inbox test trial.

    Token binds to `test_result_id`; the middleware has already validated
    the signature by the time this handler runs, but we re-verify here so
    an in-router unit test doesn't get a false pass by skipping the
    middleware layer.
    """
    if not verify_inbox_test_token(token, str(test_result_id)):
        raise HTTPException(401, "invalid or expired token")

    async with AsyncSessionLocal() as session:  # type: AsyncSession
        row = await session.get(InboxTestResult, test_result_id)
    if row is None:
        raise HTTPException(404, "test result not found")
    if not row.blob_key:
        raise HTTPException(410, "no artifact available for this trial")

    settings = get_settings()
    try:
        body = get_object(settings.s3_bucket_documents, row.blob_key)
    except Exception as e:
        log.warning(
            "inbox_test_download_blob_read_failed",
            test_id=str(test_result_id),
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(500, "artifact fetch failed") from e

    filename = f"test-{row.ordinal}.{row.ext or 'docx'}"
    return Response(
        content=body,
        media_type=_content_type(row.ext),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/downloads/case-reply/{event_id}")
async def download_case_reply(
    event_id: uuid.UUID,
    token: str = Query(..., description="Signed download token"),
) -> Response:
    """Stream the drafted reply artifact for one inbox event.

    Token is bound to the event_id via the same serializer used for job
    downloads — MCP tools can hand out a short-lived signed URL so a
    remote client (Claude Desktop) can open the PDF without shipping the
    caller's API key over the wire.
    """
    tenant_id = verify_download_token(token, str(event_id))
    if tenant_id is None:
        raise HTTPException(401, "invalid or expired token")

    async with AsyncSessionLocal() as session:
        ev = await session.get(InboxEvent, event_id)
    if ev is None or str(ev.tenant_id) != tenant_id:
        raise HTTPException(404, "case event not found")
    if not ev.reply_attachment_blob_key:
        raise HTTPException(410, "no reply artifact drafted for this event")

    settings = get_settings()
    try:
        body = get_object(settings.s3_bucket_documents, ev.reply_attachment_blob_key)
    except Exception as e:
        log.warning(
            "case_reply_download_blob_read_failed",
            event_id=str(event_id),
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(500, "artifact fetch failed") from e

    raw_outcome = (ev.pap_decision or {}).get("outcome")
    outcome = raw_outcome if raw_outcome in {"approve", "deny", "missing_info"} else "reply"
    filename = f"case_{outcome}_{str(ev.id).replace('-', '')[:8].upper()}.pdf"
    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )
