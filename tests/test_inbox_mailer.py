"""SMTP reply construction — mock smtplib.SMTP and assert the MIME shape."""
from __future__ import annotations

from email import message_from_bytes
from types import SimpleNamespace
from unittest import mock

import pytest

from docgen.inbox.mailer import Attachment, MailerError, send_reply


def _settings(**overrides):
    base = dict(
        smtp_host="smtp.example",
        smtp_port=587,
        smtp_username="bot@example.com",
        smtp_password="hunter2",
        smtp_from="bot@example.com",
        smtp_use_starttls=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _CapturingSMTP:
    """Stand-in for smtplib.SMTP that records the last send_message call."""

    instances: list["_CapturingSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_call: tuple[str, str] | None = None
        self.sent: tuple[object, str, list[str]] | None = None
        _CapturingSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        return None

    def starttls(self, context=None):
        self.starttls_called = True

    def login(self, user, password):
        self.login_call = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent = (msg, from_addr, list(to_addrs or []))


@pytest.fixture(autouse=True)
def _reset_capture():
    _CapturingSMTP.instances.clear()
    yield


def test_send_reply_headers_and_threading():
    s = _settings()
    with mock.patch("docgen.inbox.mailer.smtplib.SMTP", _CapturingSMTP):
        msg_id = send_reply(
            settings=s,
            to_addrs=["alice@customer.com"],
            cc_addrs=["ops@customer.com"],
            subject="Re: onboard template",
            body_text="hi",
            in_reply_to="<orig@customer.com>",
            references=["<earlier@customer.com>"],
        )
    assert msg_id.startswith("<") and msg_id.endswith(">")
    inst = _CapturingSMTP.instances[-1]
    assert inst.starttls_called is True
    assert inst.login_call == ("bot@example.com", "hunter2")
    sent_msg, from_addr, envelope_to = inst.sent
    assert from_addr == "bot@example.com"
    # Cc addresses must be in the envelope RCPTs (send_message won't include
    # Cc otherwise).
    assert "alice@customer.com" in envelope_to
    assert "ops@customer.com" in envelope_to
    assert sent_msg["In-Reply-To"] == "<orig@customer.com>"
    assert "<earlier@customer.com>" in sent_msg["References"]
    assert "<orig@customer.com>" in sent_msg["References"]


def test_send_reply_dedupes_cc_and_to():
    s = _settings()
    with mock.patch("docgen.inbox.mailer.smtplib.SMTP", _CapturingSMTP):
        send_reply(
            settings=s,
            to_addrs=["alice@customer.com"],
            cc_addrs=["alice@customer.com", "ALICE@customer.com", "ops@customer.com"],
            subject="Re",
            body_text="x",
        )
    inst = _CapturingSMTP.instances[-1]
    _, _, envelope_to = inst.sent
    assert envelope_to.count("alice@customer.com") == 1
    assert "ops@customer.com" in envelope_to


def test_send_reply_attachments_appear_in_mime():
    s = _settings()
    pdf_bytes = b"%PDF-fake"
    with mock.patch("docgen.inbox.mailer.smtplib.SMTP", _CapturingSMTP):
        send_reply(
            settings=s,
            to_addrs=["alice@customer.com"],
            cc_addrs=[],
            subject="Re: test",
            body_text="body",
            attachments=[
                Attachment(filename="sample.pdf", content=pdf_bytes, content_type="application/pdf"),
            ],
        )
    inst = _CapturingSMTP.instances[-1]
    sent_msg, _, _ = inst.sent
    raw = sent_msg.as_bytes()
    parsed = message_from_bytes(raw)
    attachments = [p for p in parsed.walk() if p.get_filename() == "sample.pdf"]
    assert len(attachments) == 1
    assert attachments[0].get_payload(decode=True) == pdf_bytes


def test_send_reply_requires_smtp_host():
    s = _settings(smtp_host="")
    with pytest.raises(MailerError):
        send_reply(
            settings=s,
            to_addrs=["x@y.com"],
            cc_addrs=[],
            subject="s",
            body_text="b",
        )


def test_send_reply_requires_from_addr():
    s = _settings(smtp_from="", smtp_username="")
    with pytest.raises(MailerError):
        send_reply(
            settings=s,
            to_addrs=["x@y.com"],
            cc_addrs=[],
            subject="s",
            body_text="b",
        )


def test_send_reply_requires_recipient():
    s = _settings()
    with pytest.raises(MailerError):
        send_reply(settings=s, to_addrs=[], cc_addrs=[], subject="s", body_text="b")
