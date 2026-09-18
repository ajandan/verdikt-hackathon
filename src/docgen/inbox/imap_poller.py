"""IMAP fetch of UNSEEN messages, with attachment extraction.

Thin wrapper over stdlib imaplib + email — no third-party client. The
agent needs one shot at each new mail; we mark \\Seen only after the
agent has recorded an InboxEvent row for the Message-ID, so a crash
mid-processing leaves the mail UNSEEN for the next poll to pick up.

Attachments are filtered to .docx / .pdf and capped at
`inbox_max_attachment_bytes` to keep a hostile / oversized upload from
OOMing the worker.
"""
from __future__ import annotations

import email
import imaplib
import re
import ssl
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

import structlog

from docgen.config import Settings

log = structlog.get_logger(__name__)

# Attachment types the agent will act on. Anything else in the mail is
# ignored (inline images, signatures, etc.).
_ACCEPTED_CONTENT_TYPES: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/pdf": "pdf",
    # Some clients send docx as octet-stream — filename extension fallback
    # (below) handles that case.
}


@dataclass
class InboundAttachment:
    filename: str
    content: bytes
    kind: str  # "docx" | "pdf"


@dataclass
class InboundMessage:
    message_id: str
    from_addr: str
    to_addrs: list[str]
    cc_addrs: list[str]
    subject: str
    received_at: datetime
    body_text: str
    in_reply_to: str | None
    references: list[str]
    attachments: list[InboundAttachment] = field(default_factory=list)
    uid: bytes | None = None  # IMAP UID, used to mark \Seen after processing
    # Parsed top-most ``Authentication-Results`` header verdict, per RFC 8601.
    # Populated by :func:`_parse_message`; None when the receiving MTA didn't
    # stamp one (local test messages, direct-SMTP dev harness). Consumed by
    # :func:`docgen.inbox.resolver.resolve_tenant` to gate tenant routing on
    # verified sender identity — a forged ``From:`` without matching
    # spf/dkim/dmarc PASS is refused rather than routed on the header alone.
    auth_results: "AuthResults | None" = None


@dataclass(frozen=True)
class AuthResults:
    """Parsed Authentication-Results verdict for an inbound message.

    Values are the raw RFC 8601 method-result strings (``pass``, ``fail``,
    ``neutral``, ``softfail``, ``none``, ``permerror``, ``temperror``).
    None means the header didn't carry that method. ``header_from_domain``
    is the domain the DMARC ``header.from`` (or DKIM ``d=``) tag names —
    used by the resolver to confirm the passing signature actually covers
    the visible ``From:`` header, not some unrelated domain.
    """

    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None
    header_from_domain: str | None = None
    dkim_domain: str | None = None


class ImapError(Exception):
    pass


def _decode_header_value(raw: str | None) -> str:
    if raw is None:
        return ""
    parts = email.header.decode_header(raw)
    out: list[str] = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _extract_addresses(msg: Message, header: str) -> list[str]:
    raw = msg.get_all(header, [])
    if not raw:
        return []
    return [addr for _name, addr in getaddresses(raw) if addr]


def _extract_body_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                return payload.decode("utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _classify_attachment(part: Message, max_bytes: int) -> InboundAttachment | None:
    if part.get_content_disposition() != "attachment":
        return None
    filename = _decode_header_value(part.get_filename()) or "attachment"
    ctype = (part.get_content_type() or "").lower()
    kind = _ACCEPTED_CONTENT_TYPES.get(ctype)
    if kind is None:
        # Fallback to filename extension — Outlook and Gmail sometimes send
        # docx as application/octet-stream.
        lower = filename.lower()
        if lower.endswith(".docx"):
            kind = "docx"
        elif lower.endswith(".pdf"):
            kind = "pdf"
        else:
            return None
    payload = part.get_payload(decode=True) or b""
    if len(payload) == 0:
        return None
    if len(payload) > max_bytes:
        log.warning(
            "inbox_attachment_too_large",
            filename=filename,
            bytes=len(payload),
            limit=max_bytes,
        )
        return None
    return InboundAttachment(filename=filename, content=payload, kind=kind)


def _parse_message(raw: bytes, *, uid: bytes, max_bytes: int) -> InboundMessage | None:
    msg = email.message_from_bytes(raw)

    message_id = _decode_header_value(msg.get("Message-ID"))
    if not message_id:
        # RFC-noncompliant sender; we can't dedupe without a stable ID.
        log.warning("inbox_missing_message_id", uid=uid.decode(errors="replace"))
        return None

    from_addr = parseaddr(msg.get("From", ""))[1]
    if not from_addr:
        log.warning("inbox_missing_from", message_id=message_id)
        return None

    subject = _decode_header_value(msg.get("Subject"))
    received_at = _extract_received_at(msg)

    to_addrs = _extract_addresses(msg, "To")
    cc_addrs = _extract_addresses(msg, "Cc")

    in_reply_to = _decode_header_value(msg.get("In-Reply-To")) or None
    refs_raw = _decode_header_value(msg.get("References"))
    references = [r for r in refs_raw.split() if r] if refs_raw else []

    body_text = _extract_body_text(msg)

    attachments: list[InboundAttachment] = []
    if msg.is_multipart():
        for part in msg.walk():
            att = _classify_attachment(part, max_bytes=max_bytes)
            if att is not None:
                attachments.append(att)

    return InboundMessage(
        message_id=message_id,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        subject=subject,
        received_at=received_at,
        body_text=body_text,
        in_reply_to=in_reply_to,
        references=references,
        attachments=attachments,
        uid=uid,
        auth_results=_parse_auth_results(msg),
    )


def _parse_auth_results(msg: Message) -> AuthResults | None:
    """Parse the top-most ``Authentication-Results`` header, if any.

    Multi-hop MTAs stamp one header per hop; the receiving MX puts its
    own verdict at the top, and downstream servers append below. Reading
    only the top entry keeps us honest — we can't trust verdicts stamped
    upstream of the trust boundary. ``getall`` returns headers in
    encounter order, so index 0 is the closest hop.

    Returns None when the header is absent (e.g., direct-SMTP dev
    harness or offline test fixture); callers must decide whether to
    allow un-authenticated messages.
    """
    raws = msg.get_all("Authentication-Results", [])
    if not raws:
        return None
    top = _decode_header_value(raws[0])
    if not top:
        return None

    def _method(name: str) -> str | None:
        m = re.search(rf"\b{name}\s*=\s*([A-Za-z]+)", top, re.IGNORECASE)
        return m.group(1).lower() if m else None

    def _prop(prop: str) -> str | None:
        m = re.search(
            rf"{re.escape(prop)}\s*=\s*([A-Za-z0-9.\-_@]+)",
            top,
            re.IGNORECASE,
        )
        return m.group(1).lower() if m else None

    return AuthResults(
        spf=_method("spf"),
        dkim=_method("dkim"),
        dmarc=_method("dmarc"),
        header_from_domain=_prop("header.from"),
        dkim_domain=_prop("header.d"),
    )


def _extract_received_at(msg: Message) -> datetime:
    raw = msg.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


class ImapPoller:
    """Connect → SELECT → SEARCH UNSEEN → yield → mark \\Seen on ack."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ImapPoller":
        s = self._settings
        if not (s.inbox_imap_host and s.inbox_username and s.inbox_password):
            raise ImapError("IMAP not configured")
        context = ssl.create_default_context()
        try:
            self._conn = imaplib.IMAP4_SSL(
                s.inbox_imap_host, s.inbox_imap_port, ssl_context=context, timeout=30
            )
            self._conn.login(s.inbox_username, s.inbox_password)
            typ, _ = self._conn.select(s.inbox_mailbox)
            if typ != "OK":
                raise ImapError(f"SELECT {s.inbox_mailbox} failed")
        except (imaplib.IMAP4.error, OSError) as e:
            self._close_silent()
            raise ImapError(f"IMAP connect failed: {type(e).__name__}: {e}") from e
        return self

    def __exit__(self, *_exc: object) -> None:
        self._close_silent()

    def _close_silent(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._conn.logout()
        except Exception:
            pass
        self._conn = None

    def fetch_unseen(self) -> Iterator[InboundMessage]:
        assert self._conn is not None
        typ, data = self._conn.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            raise ImapError(f"SEARCH failed: {data}")
        uids = data[0].split() if data and data[0] else []
        max_bytes = self._settings.inbox_max_attachment_bytes
        for uid in uids:
            typ, payload = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not payload:
                log.warning("inbox_fetch_failed", uid=uid.decode(errors="replace"))
                continue
            raw_bytes: bytes | None = None
            for chunk in payload:
                if isinstance(chunk, tuple) and len(chunk) >= 2 and isinstance(chunk[1], (bytes, bytearray)):
                    raw_bytes = bytes(chunk[1])
                    break
            if raw_bytes is None:
                continue
            parsed = _parse_message(raw_bytes, uid=uid, max_bytes=max_bytes)
            if parsed is None:
                # Non-parseable / no Message-ID — mark seen so we don't
                # keep re-fetching the same broken message every tick.
                self.mark_seen(uid)
                continue
            yield parsed

    def mark_seen(self, uid: bytes) -> None:
        assert self._conn is not None
        try:
            self._conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        except imaplib.IMAP4.error as e:
            log.warning(
                "inbox_mark_seen_failed",
                uid=uid.decode(errors="replace"),
                error=str(e),
            )
