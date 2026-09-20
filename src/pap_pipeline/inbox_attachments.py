"""Inbound-attachment I/O for the PAP inbox pipeline.

Extracted from :mod:`pap_pipeline.inbox_hook` — both helpers are
attachment-only and share a coherent responsibility (pull bytes out of
the poller's InboundMessage, then upload each attachment to the
documents blob store with a stable key layout).

The blob store is best-effort: a persist failure logs and continues so
the claim still runs, at the cost of a missing preview link in the UI.
The bytes destined for the pipeline are already in memory by the time
the upload runs, so an S3 outage does not break the extraction step.
"""
from __future__ import annotations

import re
from typing import Any

import structlog

from docgen.config import Settings
from docgen.inbox.imap_poller import InboundMessage
from docgen.store.blob import put_object
from docgen.store.models import InboxEvent

log = structlog.get_logger(__name__)


# InboundAttachment.kind is docx|pdf; the poller filters everything else,
# so this map covers the only two we ever land on. Falls back to the
# generic bytestream MIME so a future third kind still gets served.
_KIND_TO_CONTENT_TYPE = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# Anything that isn't [alnum . _ -] gets collapsed to `_`. Blob stores
# tolerate `..` in keys but any future filesystem-backed driver would
# read it as a path-traversal segment, and control chars / NUL crash
# some object-store SDKs outright. Restricting to a conservative
# allowlist keeps the key portable across S3, GCS, and local disk.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SAFE_NAME_LEN = 128


def _sanitize_attachment_filename(raw: str, idx: int) -> str:
    """Return a blob-key-safe rendering of an attachment filename.

    Strips path separators, `..`, control chars, and other non-portable
    characters. Empty or purely-unsafe inputs fall back to
    ``attachment-<idx>``. Length-capped so a rogue 4 KB filename can't
    balloon the key.
    """
    base = raw or f"attachment-{idx}"
    # Collapse any run of unsafe chars (including `/`, `\`, `..` runs,
    # control chars, NUL, unicode oddities) to a single underscore.
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", base).strip("._") or f"attachment-{idx}"
    return cleaned[:_MAX_SAFE_NAME_LEN]


def attachments_from_message(msg: InboundMessage) -> list[bytes]:
    """Every attachment goes to the pipeline — a claim often spans a pay stub,
    an insurance card, and a prescription across separate files."""
    return [a.content for a in msg.attachments if a.content]


def persist_inbound_attachments(
    event: InboxEvent, msg: InboundMessage, settings: Settings
) -> list[dict[str, Any]]:
    """Upload each inbound attachment to the documents bucket and return a
    manifest the UI can render as preview links.

    Blob-store failures don't abort the pipeline — a missing preview is less
    bad than dropping the claim. Each failed upload just gets its blob_key
    left null in the manifest so the UI renders it as "(upload failed)".
    """
    manifest: list[dict[str, Any]] = []
    for idx, a in enumerate(msg.attachments):
        if not a.content:
            continue
        content_type = _KIND_TO_CONTENT_TYPE.get(a.kind, "application/octet-stream")
        safe_name = _sanitize_attachment_filename(a.filename or "", idx)
        blob_key = f"inbox-inbound/{event.id}/{idx:02d}-{safe_name}"
        entry: dict[str, Any] = {
            "filename": a.filename or safe_name,
            "content_type": content_type,
            "size": len(a.content),
            "blob_key": None,
        }
        try:
            put_object(
                settings.s3_bucket_documents,
                blob_key,
                a.content,
                content_type=content_type,
            )
            entry["blob_key"] = blob_key
        except Exception as e:
            log.warning(
                "pap_inbound_attachment_persist_failed",
                event_id=str(event.id),
                filename=a.filename,
                error=f"{type(e).__name__}: {e}",
                exc_info=True,
            )
        manifest.append(entry)
    return manifest


__all__ = ["attachments_from_message", "persist_inbound_attachments"]
