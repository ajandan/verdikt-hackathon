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

from docgen.auth import verify_inbox_test_token
from docgen.config import get_settings
from docgen.store.blob import get_object
from docgen.store.db import AsyncSessionLocal
from docgen.store.models import InboxTestResult

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
