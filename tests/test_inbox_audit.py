"""Audit-trail coverage for the inbox agent's reply branches.

Each user-visible decision an operator can trace later — approve, reject,
auto-approve, spoofed-reply — must land a `template_audit_events` row
against the target template. These tests drive the four branches with
in-memory stubs (no DB, no network) and assert the audit helper is
invoked with the expected `event_kind` and `actor` shape.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from docgen.inbox import agent
from docgen.inbox.imap_poller import InboundMessage
from docgen.store.models import InboxEvent


class _StubSession:
    """Just enough Session surface for _process_reply to run.

    _record_reply_event constructs an InboxEvent, adds it, commits, then
    refreshes it. We fake refresh by giving the row an id inline.
    """

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)
        # Assign an id on first add so `.refresh()` looks like it happened.
        if isinstance(obj, InboxEvent) and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, obj: object) -> None:
        return None


def _msg(from_addr: str = "alice@customer.com") -> InboundMessage:
    return InboundMessage(
        message_id="<reply-123@x>",
        from_addr=from_addr,
        to_addrs=["quire.template.agent@example.com"],
        cc_addrs=[],
        subject="Re: your template",
        received_at=datetime.now(UTC),
        body_text="APPROVE",
        in_reply_to="<parent-abc@x>",
        references=["<parent-abc@x>"],
        attachments=[],
    )


def _parent(
    from_addr: str = "alice@customer.com",
    review_state: str | None = "pending_review",
) -> InboxEvent:
    ev = InboxEvent(
        id=uuid.uuid4(),
        message_id="<parent-abc@x>",
        from_addr=from_addr,
        cc_addrs=[],
        subject="Please onboard this template",
        received_at=datetime.now(UTC),
        status="succeeded",
        tenant_id=uuid.uuid4(),
        template_id=uuid.uuid4(),
        template_version_id=uuid.uuid4(),
        review_state=review_state,
    )
    return ev


@pytest.fixture
def audit_spy(monkeypatch):
    """Capture every _write_template_audit call as a dict of kwargs."""
    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(agent, "_write_template_audit", _spy)
    return calls


@pytest.fixture
def silence_smtp(monkeypatch):
    """Neutralize outbound mail — replies aren't under test here."""
    monkeypatch.setattr(agent, "_send_reply_ack", lambda *a, **kw: None)


@pytest.fixture
def stub_settings():
    # _process_reply only touches settings via _send_reply_ack + publish flow;
    # a bare namespace suffices once those are mocked.
    return SimpleNamespace(smtp_host="smtp.example.com")


def test_reject_writes_audit_event(audit_spy, silence_smtp, stub_settings):
    session = _StubSession()
    parent = _parent()
    msg = _msg()
    msg.body_text = "REJECT"

    result = agent._process_reply(session, msg=msg, parent=parent, settings=stub_settings)

    assert result.status == "succeeded"
    assert result.detail == "rejected"
    assert parent.review_state == "rejected"
    assert len(audit_spy) == 1
    call = audit_spy[0]
    assert call["event_kind"] == "inbox_rejected"
    assert call["template_id"] == parent.template_id
    assert call["template_version_id"] == parent.template_version_id
    assert call["actor"] == f"reply-reject:{msg.from_addr}"
    assert call["summary"]["inbox_event_id"] == str(parent.id)


def test_approve_writes_audit_event(monkeypatch, audit_spy, silence_smtp, stub_settings):
    # The approve branch calls publish_version through a nested async closure;
    # short-circuit that so we exercise only the audit-write path. Close the
    # coroutine we're skipping so it doesn't emit a "never awaited" warning.
    def _skip(coro):
        coro.close()
        return (1, 0)

    monkeypatch.setattr(agent, "_run_async", _skip)

    session = _StubSession()
    parent = _parent()
    msg = _msg()
    msg.body_text = "APPROVE"

    result = agent._process_reply(session, msg=msg, parent=parent, settings=stub_settings)

    assert result.status == "succeeded"
    assert result.detail == "approved"
    assert parent.review_state == "approved"
    assert len(audit_spy) == 1
    call = audit_spy[0]
    assert call["event_kind"] == "inbox_approved"
    assert call["actor"] == f"reply-approve:{msg.from_addr}"


def test_sender_mismatch_writes_audit_event(audit_spy, silence_smtp, stub_settings):
    session = _StubSession()
    parent = _parent(from_addr="alice@customer.com")
    msg = _msg(from_addr="stranger@evil.com")
    msg.body_text = "APPROVE"

    result = agent._process_reply(session, msg=msg, parent=parent, settings=stub_settings)

    assert result.status == "skipped"
    assert "sender_mismatch" in result.detail
    # Parent state must NOT flip — spoofed reply.
    assert parent.review_state == "pending_review"
    assert len(audit_spy) == 1
    call = audit_spy[0]
    assert call["event_kind"] == "inbox_sender_mismatch"
    assert call["actor"] == f"spoofed:{msg.from_addr}"
    assert call["summary"]["expected_from"] == parent.from_addr


def test_ambiguous_reply_writes_no_audit(audit_spy, silence_smtp, stub_settings):
    """A human comment (no APPROVE/REJECT keyword) is not a decision — no audit."""
    session = _StubSession()
    parent = _parent()
    msg = _msg()
    msg.body_text = "hey can you double check this?"

    result = agent._process_reply(session, msg=msg, parent=parent, settings=stub_settings)

    assert result.status == "skipped"
    assert result.detail == "no_keyword"
    assert audit_spy == []


def test_write_template_audit_noop_when_no_template(monkeypatch):
    """The helper must never open a session when there's nothing to audit."""
    calls = []

    def _run(_coro):
        calls.append("ran")

    monkeypatch.setattr(agent, "_run_async", _run)

    agent._write_template_audit(
        template_id=None,
        template_version_id=None,
        event_kind="inbox_rejected",
        actor="reply-reject:x@y",
    )
    assert calls == []
