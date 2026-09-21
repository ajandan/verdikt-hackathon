"""SMTP send with attachments + RFC 5322 threading.

Small deliberate surface: one `send_reply` call that takes a parsed
inbound message and everything the agent wants to add. Threading uses
In-Reply-To + References so replies land in the same conversation view
in the sender's client, which is what makes the "reply APPROVE" flow
(Phase 3) natural.

Uses stdlib smtplib + email — no extra deps, no async wrapping. The
Dramatiq actor that calls this is sync anyway.
"""
from __future__ import annotations

import smtplib
import ssl
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import structlog

from docgen.config import Settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    content_type: str  # e.g. "application/pdf"


class MailerError(Exception):
    pass


def _reject_crlf(value: str, *, kind: str) -> str:
    """Refuse any address / subject / header value that carries CR or LF.

    Python's :class:`email.message.EmailMessage` folds long headers but
    does not scrub embedded newlines when a header value is set as a
    plain string. A caller that lets attacker-controlled text
    (an email subject, an applicant-supplied CC address) reach this
    module verbatim could otherwise inject an extra ``Bcc:`` or
    similar header via ``\\r\\n``. Raising is proportionate: legitimate
    values never contain CR/LF; if one shows up it's a bug or an
    attack.
    """
    if "\r" in value or "\n" in value:
        raise MailerError(f"{kind} contains illegal newline")
    return value


def _unique(addrs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in addrs:
        a = (a or "").strip()
        if not a:
            continue
        _reject_crlf(a, kind="address")
        key = a.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def send_reply(
    *,
    settings: Settings,
    to_addrs: list[str],
    cc_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachments: list[Attachment] | None = None,
    from_addr: str | None = None,
) -> str:
    """Send an SMTP email. Returns the Message-ID of the sent message."""
    host = settings.smtp_host
    if not host:
        raise MailerError("SMTP not configured (DOCGEN_SMTP_HOST is empty)")

    sender = (from_addr or settings.smtp_from or settings.smtp_username or "").strip()
    if not sender:
        raise MailerError("No sender address (set DOCGEN_SMTP_FROM or DOCGEN_SMTP_USERNAME)")
    _reject_crlf(sender, kind="sender")
    _reject_crlf(subject or "", kind="subject")
    if in_reply_to:
        _reject_crlf(in_reply_to, kind="in_reply_to")

    to_final = _unique(to_addrs)
    cc_final = _unique(a for a in cc_addrs if a not in to_final)
    if not to_final:
        raise MailerError("send_reply requires at least one To address")

    msg = EmailMessage()
    # Domain of the sender for a well-formed Message-ID — some receivers
    # will drop messages with a mismatched msgid domain.
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else "localhost"
    msg_id = make_msgid(idstring=uuid.uuid4().hex[:12], domain=domain)
    msg["Message-ID"] = msg_id
    msg["From"] = sender
    msg["To"] = ", ".join(to_final)
    if cc_final:
        msg["Cc"] = ", ".join(cc_final)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # References chains previous IDs; RFC 5322 says append the parent.
        refs = list(references or [])
        if in_reply_to not in refs:
            refs.append(in_reply_to)
        msg["References"] = " ".join(refs)

    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    for att in attachments or []:
        maintype, _, subtype = (att.content_type or "application/octet-stream").partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            att.content,
            maintype=maintype,
            subtype=subtype,
            filename=att.filename,
        )

    envelope_recipients = to_final + cc_final
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(host, settings.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if settings.smtp_use_starttls:
                smtp.starttls(context=context)
                smtp.ehlo()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg, from_addr=sender, to_addrs=envelope_recipients)
    except (smtplib.SMTPException, OSError) as e:
        raise MailerError(f"SMTP send failed: {type(e).__name__}: {e}") from e

    log.info(
        "inbox_reply_sent",
        message_id=msg_id,
        to=to_final,
        cc=cc_final,
        subject=subject,
        in_reply_to=in_reply_to,
        attachments=len(attachments or []),
    )
    return msg_id
