"""IMAP payload parsing — feed a synthetic RFC 822 bytes blob and check the
extracted InboundMessage. No network, no IMAP server.
"""
from __future__ import annotations

from email.message import EmailMessage

from docgen.inbox.imap_poller import _parse_message


def _wire_bytes(msg: EmailMessage) -> bytes:
    return msg.as_bytes()


def _base_message(**overrides) -> EmailMessage:
    m = EmailMessage()
    m["Message-ID"] = overrides.get("message_id", "<abc123@customer.com>")
    m["From"] = overrides.get("from_", "Alice <alice@customer.com>")
    m["To"] = overrides.get("to", "intake@vendor.com")
    m["Subject"] = overrides.get("subject", "Onboard template")
    m.set_content("Please onboard the attached template.")
    return m


def test_parse_extracts_headers_and_body():
    m = _base_message()
    parsed = _parse_message(_wire_bytes(m), uid=b"12", max_bytes=1_000_000)
    assert parsed is not None
    assert parsed.message_id == "<abc123@customer.com>"
    assert parsed.from_addr == "alice@customer.com"
    assert parsed.subject == "Onboard template"
    assert "onboard" in parsed.body_text.lower()
    assert parsed.attachments == []


def test_parse_missing_message_id_returns_none():
    m = _base_message()
    del m["Message-ID"]
    parsed = _parse_message(_wire_bytes(m), uid=b"1", max_bytes=1_000_000)
    assert parsed is None


def test_parse_pdf_attachment_by_content_type():
    m = _base_message()
    m.add_attachment(b"%PDF-fake", maintype="application", subtype="pdf", filename="form.pdf")
    parsed = _parse_message(_wire_bytes(m), uid=b"2", max_bytes=1_000_000)
    assert parsed is not None
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.kind == "pdf"
    assert att.filename == "form.pdf"
    assert att.content == b"%PDF-fake"


def test_parse_docx_attachment_by_extension_fallback():
    # Outlook / Gmail sometimes send docx as octet-stream; the parser must
    # fall back on the .docx extension.
    m = _base_message()
    m.add_attachment(
        b"PK-fake",
        maintype="application",
        subtype="octet-stream",
        filename="template.docx",
    )
    parsed = _parse_message(_wire_bytes(m), uid=b"3", max_bytes=1_000_000)
    assert parsed is not None
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].kind == "docx"


def test_parse_oversized_attachment_is_dropped():
    m = _base_message()
    big = b"A" * 2048
    m.add_attachment(big, maintype="application", subtype="pdf", filename="huge.pdf")
    parsed = _parse_message(_wire_bytes(m), uid=b"4", max_bytes=1024)
    assert parsed is not None
    assert parsed.attachments == []


def test_parse_ignores_inline_images():
    m = _base_message()
    # add_related is inline, not an attachment.
    m.add_related(b"\x89PNG-fake", maintype="image", subtype="png", cid="logo1")
    parsed = _parse_message(_wire_bytes(m), uid=b"5", max_bytes=1_000_000)
    assert parsed is not None
    assert parsed.attachments == []


def test_parse_cc_addresses_extracted():
    m = _base_message()
    m["Cc"] = "Bob <bob@customer.com>, carol@customer.com"
    parsed = _parse_message(_wire_bytes(m), uid=b"6", max_bytes=1_000_000)
    assert parsed is not None
    assert set(parsed.cc_addrs) == {"bob@customer.com", "carol@customer.com"}


def test_parse_in_reply_to_and_references():
    m = _base_message()
    m["In-Reply-To"] = "<prev1@customer.com>"
    m["References"] = "<prev0@customer.com> <prev1@customer.com>"
    parsed = _parse_message(_wire_bytes(m), uid=b"7", max_bytes=1_000_000)
    assert parsed is not None
    assert parsed.in_reply_to == "<prev1@customer.com>"
    assert parsed.references == ["<prev0@customer.com>", "<prev1@customer.com>"]
