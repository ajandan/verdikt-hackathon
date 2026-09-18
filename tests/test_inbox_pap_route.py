"""[pap] subject-tag routing — dispatches to the registered plugin handler.

Two guarantees the inbox agent has to hold up:
  1. A `[pap]` (optionally `[pap:program]`) subject routes to `_pap_handler`
     and skips the template-ingest branch entirely.
  2. When the tag is present but no plugin registered the slot, the sender
     gets a clear reject reply — silence would be a debugging nightmare.

Also covers the pure detector regexes because a false positive on the
subject-tag pattern would silently divert unrelated template mail.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from docgen.inbox import agent as agent_mod
from docgen.inbox.agent import (
    ProcessResult,
    _pap_program_from_subject,
    clear_pap_handler,
    process_message,
    set_pap_handler,
)
from docgen.inbox.imap_poller import InboundAttachment, InboundMessage


@pytest.fixture(autouse=True)
def _reset_handler():
    clear_pap_handler()
    yield
    clear_pap_handler()


def test_detector_matches_bare_tag() -> None:
    matched, program = _pap_program_from_subject("[pap] my application")
    assert matched is True
    assert program is None


def test_detector_matches_tag_with_program() -> None:
    matched, program = _pap_program_from_subject("[PAP:cardiovita-pap-2026] hi")
    assert matched is True
    assert program == "cardiovita-pap-2026"


def test_detector_ignores_regular_subject() -> None:
    matched, program = _pap_program_from_subject("Denial letter template v3")
    assert matched is False
    assert program is None


def test_detector_handles_none_subject() -> None:
    assert _pap_program_from_subject(None) == (False, None)


def _msg(subject: str) -> InboundMessage:
    return InboundMessage(
        message_id=f"<{uuid.uuid4()}@t>",
        from_addr="applicant@example.com",
        to_addrs=["intake@quire.example"],
        cc_addrs=[],
        subject=subject,
        received_at=datetime.now(tz=UTC),
        body_text="Please review my PAP application.",
        in_reply_to=None,
        references=[],
        attachments=[InboundAttachment(filename="p.png", content=b"\x89PNG", kind="pdf")],
    )


class _FakeSession:
    """Stand-in for a SQLAlchemy Session — enough surface for the branch.

    Records the InboxEvent-like object added so the test can assert on the
    fields the branch mutates before dispatch.
    """

    def __init__(self, tenant) -> None:
        self._tenant = tenant
        self.added: list = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)
        # Emulate Base default-value fill so downstream reads don't KeyError.
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, obj) -> None:
        pass

    def query(self, _model):
        session = self

        class _Q:
            def all(self_inner):
                return [session._tenant]

        return _Q()


def _patch_resolver(monkeypatch, tenant) -> None:
    match = SimpleNamespace(tenant=tenant, matched_by="subject_tag")
    monkeypatch.setattr(
        agent_mod,
        "resolve_tenant",
        lambda tenants, from_addr, subject, auth_results=None: match,
    )


def test_pap_subject_dispatches_to_registered_handler(monkeypatch) -> None:
    """When [pap] is present and a handler is registered, the branch calls
    it and returns its ProcessResult unchanged."""
    tenant = SimpleNamespace(id=uuid.uuid4(), ai_provider_config=None)
    session = _FakeSession(tenant)
    _patch_resolver(monkeypatch, tenant)

    called: dict = {}

    def _handler(sess, *, event, msg, tenant, program, settings):
        called["sess"] = sess
        called["event_id"] = event.id
        called["subject"] = msg.subject
        called["tenant_id"] = tenant.id
        called["program"] = program
        return ProcessResult(event.id, "succeeded", "approve")

    set_pap_handler(_handler)

    result = process_message(
        session,  # type: ignore[arg-type]
        msg=_msg("[pap:cardiovita-pap-2026] my claim"),
    )

    assert called["subject"].startswith("[pap")
    assert called["program"] == "cardiovita-pap-2026"
    assert called["tenant_id"] == tenant.id
    assert result.status == "succeeded"
    assert result.detail == "approve"


def test_pap_subject_without_handler_skips_with_error(monkeypatch) -> None:
    """A [pap] tag with no plugin registered must skip with a clear error
    rather than fall through to the template-ingest branch."""
    tenant = SimpleNamespace(id=uuid.uuid4(), ai_provider_config=None)
    session = _FakeSession(tenant)
    _patch_resolver(monkeypatch, tenant)
    # Prevent the reject-reply path from attempting SMTP during the test.
    monkeypatch.setattr(agent_mod, "_try_send_reject_reply", lambda *a, **k: None)

    result = process_message(
        session,  # type: ignore[arg-type]
        msg=_msg("[pap] my claim"),
    )

    assert result.status == "skipped"
    assert "pap handler" in result.detail.lower()


def test_regular_subject_bypasses_pap_branch(monkeypatch) -> None:
    """A subject without [pap] must NOT invoke the handler even when one is
    registered — the template flow keeps working."""
    tenant = SimpleNamespace(id=uuid.uuid4(), ai_provider_config=None)
    session = _FakeSession(tenant)
    _patch_resolver(monkeypatch, tenant)

    called = {"n": 0}

    def _handler(*args, **kwargs):
        called["n"] += 1
        return ProcessResult(uuid.uuid4(), "succeeded", "ok")

    set_pap_handler(_handler)
    # Short-circuit the template flow — we don't need it to succeed, just
    # avoid its DB/network work. `_pick_attachment` returning None sends the
    # request down the "no .docx/.pdf attachment" skip branch.
    monkeypatch.setattr(agent_mod, "_pick_attachment", lambda _msg: None)
    monkeypatch.setattr(agent_mod, "_try_send_reject_reply", lambda *a, **k: None)

    result = process_message(
        session,  # type: ignore[arg-type]
        msg=_msg("Please onboard this template"),
    )

    assert called["n"] == 0
    assert result.status == "skipped"
