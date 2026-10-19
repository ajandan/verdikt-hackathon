"""Reply-artifact + reply-send lifecycle tests for the PAP inbox hook.

Complements ``test_pap_inbox_hook.py`` (slot resolution). The three
functions under test — ``_score_from_decision``, ``_draft_reply_artifact``,
``_send_pap_reply_for_event`` — form the seam between "pipeline
produced a Decision" and "applicant receives an email." Prior to this
file the entire approve-and-send path had zero coverage.

Mocks live at the module boundary: session is a MagicMock, and the
imported helpers (``assemble_reply_package``, ``build_render_data``,
``render_reply_attachment``, ``get_object``, ``send_reply``) are
monkey-patched on the ``pap_pipeline.inbox_hook`` namespace so the
tests exercise the real branching without needing S3, SMTP, or a
frozen template.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from docgen.inbox.mailer import Attachment, MailerError
from docgen.store.models import InboxEvent, Program, Tenant
from pap_pipeline import inbox_hook
from pap_pipeline.assembler import (
    AssemblerError,
    AssemblerMissingBinding,
    AssemblerRenderFailed,
    AssemblyResult,
)
from pap_pipeline.inbox_hook import (
    _draft_reply_artifact,
    _score_from_decision,
    _send_pap_reply_for_event,
)
from pap_pipeline.schemas.decision import Citation, Decision
from pap_pipeline.schemas.intake import Intake

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tenant() -> Tenant:
    return Tenant(id=uuid.uuid4(), name="acme", ai_provider_config={})


def _event(
    *,
    from_addr: str = "applicant@example.com",
    subject: str = "PAP intake",
    decision_dict: dict | None = None,
    reply_attachment_blob_key: str | None = None,
) -> InboxEvent:
    return InboxEvent(
        id=uuid.uuid4(),
        message_id="<orig@x>",
        from_addr=from_addr,
        cc_addrs=[],
        subject=subject,
        received_at=datetime.now(UTC),
        status="succeeded",
        pap_decision=decision_dict,
        reply_attachment_blob_key=reply_attachment_blob_key,
    )


def _rule_citation(rule_id: str, matched: bool, score: float | None) -> Citation:
    return Citation(type="rule", rule_id=rule_id, matched=matched, score=score)


def _history_citation() -> Citation:
    return Citation(
        type="history",
        event_id=str(uuid.uuid4()),
        slot_name="prior_enrollment",
        snapshot_hash="deadbeef",
    )


def _decision(
    *,
    outcome: str = "approve",
    citations: list[Citation] | None = None,
    missing_fields: list[str] | None = None,
    rationale: str = "",
) -> Decision:
    return Decision(
        outcome=outcome,  # type: ignore[arg-type]
        citations=citations or [],
        missing_fields=missing_fields or [],
        rationale=rationale,
    )


def _intake() -> Intake:
    return Intake()


def _settings(**overrides) -> MagicMock:
    """Minimal Settings shape — only the fields the tested code touches.

    A real Settings requires a bunch of env-var-driven fields; a MagicMock
    with the specific attributes lets us skip that setup.
    """
    settings = MagicMock()
    settings.s3_bucket_documents = "test-bucket"
    settings.smtp_host = "smtp.test"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _session_returning(program_row: Program | None) -> MagicMock:
    """`session.query(Program).filter(...).first()` returns `program_row`."""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = program_row
    return session


# ---------------------------------------------------------------------------
# _score_from_decision — pure function, no mocks
# ---------------------------------------------------------------------------


def test_score_none_when_no_citations():
    assert _score_from_decision(_decision(citations=[])) is None


def test_score_none_when_only_history_citations():
    """History citations don't carry retrieval scores by design (ADR-0004).
    A decision with only history evidence should score None, not zero."""
    dec = _decision(citations=[_history_citation(), _history_citation()])
    assert _score_from_decision(dec) is None


def test_score_none_when_rule_citations_have_null_score():
    """Rules that fired but weren't scored (deterministic-only rules,
    or the score-attach pass didn't run) leave score=None."""
    dec = _decision(citations=[
        _rule_citation("R-001", matched=True, score=None),
        _rule_citation("R-002", matched=False, score=None),
    ])
    assert _score_from_decision(dec) is None


def test_score_picks_max_and_scales_to_hundred():
    """Retrieval scores are 0..1; UI column expects 0..100 with one dp."""
    dec = _decision(citations=[
        _rule_citation("R-001", matched=True, score=0.42),
        _rule_citation("R-002", matched=False, score=0.876),
        _rule_citation("R-003", matched=True, score=0.5),
    ])
    assert _score_from_decision(dec) == 87.6


def test_score_ignores_history_citations_when_rule_scores_exist():
    """Mixed evidence — history rows must not dilute the pick-max."""
    dec = _decision(citations=[
        _history_citation(),
        _rule_citation("R-001", matched=True, score=0.9),
        _history_citation(),
    ])
    assert _score_from_decision(dec) == 90.0


# ---------------------------------------------------------------------------
# _draft_reply_artifact
# ---------------------------------------------------------------------------


def _patch_reply_render(monkeypatch, blob_key: str = "legacy-blob-key"):
    """Stub the reply-render helpers that ``_draft_reply_artifact`` calls."""
    monkeypatch.setattr(inbox_hook, "build_render_data", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(
        inbox_hook,
        "render_reply_attachment",
        lambda *a, **kw: (Attachment(filename="x.pdf", content=b"pdf", content_type="application/pdf"), blob_key),
    )


def test_draft_reply_artifact_assembler_success(monkeypatch):
    """Program has a binding for the outcome and the assembler runs cleanly:
    return the assembler's blob_key, never touch the legacy render path."""
    program_row = MagicMock(spec=Program)
    program_row.package_bindings = {"approve": str(uuid.uuid4())}
    session = _session_returning(program_row)

    assembler_blob = "packages/tenant/pkg-42/approve.pdf"
    monkeypatch.setattr(inbox_hook, "build_render_data", lambda *a, **kw: {})
    monkeypatch.setattr(
        inbox_hook,
        "assemble_reply_package",
        lambda *a, **kw: AssemblyResult(
            pdf_bytes=b"pdf", filename="out.pdf",
            blob_key=assembler_blob, assembly_id=uuid.uuid4(),
            page_count=1, byte_size=3, item_count=1,
        ),
    )
    # Legacy render must not fire — assert by tripping a test failure if it does.
    monkeypatch.setattr(
        inbox_hook,
        "render_reply_attachment",
        lambda *a, **kw: pytest.fail("legacy render must not run on assembler success"),
    )

    event = _event()
    result = _draft_reply_artifact(
        session,
        _tenant(),
        _intake(),
        _decision(outcome="approve"),
        program="acme-pap",
        event=event,
        settings=_settings(),
        extras=None,
    )
    assert result == assembler_blob
    # No degradation audit — the assembler succeeded.
    assert not any(
        entry.get("action") == "reply_assembler_degraded"
        for entry in (event.review_history or [])
    )


def test_draft_reply_artifact_assembler_error_falls_back_and_audits(monkeypatch):
    """AssemblerRenderFailed inside the assembler falls through to legacy render.
    The reviewer's timeline gets a `reply_assembler_degraded` entry so the
    package-config failure isn't invisible behind a "reply drafted" line."""
    program_row = MagicMock(spec=Program)
    program_row.package_bindings = {"approve": str(uuid.uuid4())}
    session = _session_returning(program_row)

    def _boom(*a, **kw):
        raise AssemblerRenderFailed("item 3 missing static doc version")

    monkeypatch.setattr(inbox_hook, "assemble_reply_package", _boom)
    _patch_reply_render(monkeypatch, blob_key="fallback-blob")

    event = _event()
    result = _draft_reply_artifact(
        session,
        _tenant(),
        _intake(),
        _decision(outcome="approve"),
        program="acme-pap",
        event=event,
        settings=_settings(),
        extras=None,
    )
    assert result == "fallback-blob"
    degraded = [
        entry for entry in event.review_history
        if entry.get("action") == "reply_assembler_degraded"
    ]
    assert len(degraded) == 1
    assert "AssemblerRenderFailed" in degraded[0]["note"]
    assert "item 3 missing" in degraded[0]["note"]


def test_draft_reply_artifact_missing_binding_falls_back_and_audits(monkeypatch):
    """AssemblerMissingBinding (raised inside the assembler despite our
    upstream check) also degrades to legacy render with an audit entry.
    Regression guard: reviewer sees the config drift even in this rare race."""
    program_row = MagicMock(spec=Program)
    program_row.package_bindings = {"approve": str(uuid.uuid4())}
    session = _session_returning(program_row)

    def _boom(*a, **kw):
        raise AssemblerMissingBinding("binding disappeared mid-flight")

    monkeypatch.setattr(inbox_hook, "assemble_reply_package", _boom)
    _patch_reply_render(monkeypatch, blob_key="fallback-blob")

    event = _event()
    result = _draft_reply_artifact(
        session, _tenant(), _intake(),
        _decision(outcome="approve"), program="acme-pap",
        event=event, settings=_settings(), extras=None,
    )
    assert result == "fallback-blob"
    degraded = [
        entry for entry in event.review_history
        if entry.get("action") == "reply_assembler_degraded"
    ]
    assert len(degraded) == 1
    assert "binding disappeared" in degraded[0]["note"]


def test_draft_reply_artifact_no_program_row_uses_legacy_render(monkeypatch):
    """Slug names a program that isn't registered in the DB — skip the
    assembler entirely, use legacy render, no degradation audit (nothing
    was expected to degrade)."""
    session = _session_returning(program_row=None)

    # Fail loudly if the assembler is even considered.
    monkeypatch.setattr(
        inbox_hook,
        "assemble_reply_package",
        lambda *a, **kw: pytest.fail("assembler must not run without a program row"),
    )
    _patch_reply_render(monkeypatch, blob_key="fallback-blob")

    event = _event()
    result = _draft_reply_artifact(
        session, _tenant(), _intake(),
        _decision(outcome="approve"), program="unknown-slug",
        event=event, settings=_settings(), extras=None,
    )
    assert result == "fallback-blob"
    assert not any(
        entry.get("action") == "reply_assembler_degraded"
        for entry in (event.review_history or [])
    )


def test_draft_reply_artifact_program_without_binding_uses_legacy_render(monkeypatch):
    """Program exists but has no binding for this specific outcome — skip
    the assembler, use legacy render. This is the "tenant hasn't declared
    outcomes yet" migration path, not a failure."""
    program_row = MagicMock(spec=Program)
    program_row.package_bindings = {"deny": str(uuid.uuid4())}  # no 'approve'
    session = _session_returning(program_row)

    monkeypatch.setattr(
        inbox_hook,
        "assemble_reply_package",
        lambda *a, **kw: pytest.fail("assembler must not run without a matching binding"),
    )
    _patch_reply_render(monkeypatch, blob_key="fallback-blob")

    event = _event()
    result = _draft_reply_artifact(
        session, _tenant(), _intake(),
        _decision(outcome="approve"), program="acme-pap",
        event=event, settings=_settings(), extras=None,
    )
    assert result == "fallback-blob"
    assert not any(
        entry.get("action") == "reply_assembler_degraded"
        for entry in (event.review_history or [])
    )


# ---------------------------------------------------------------------------
# _send_pap_reply_for_event
# ---------------------------------------------------------------------------


def test_send_reply_raises_when_no_decision():
    event = _event(decision_dict=None)
    with pytest.raises(MailerError, match="no pipeline decision"):
        _send_pap_reply_for_event(MagicMock(), event, _settings())


def test_send_reply_raises_when_smtp_not_configured(monkeypatch):
    """Empty DOCGEN_SMTP_HOST → refuse to send. Reviewer sees the
    misconfiguration back on the approve page rather than a silent no-op."""
    event = _event(decision_dict=_decision(outcome="approve").model_dump(mode="json"))
    with pytest.raises(MailerError, match="SMTP not configured"):
        _send_pap_reply_for_event(MagicMock(), event, _settings(smtp_host=""))


def test_send_reply_text_only_when_no_blob_key(monkeypatch):
    """No reply_attachment_blob_key on the event → send a text-only reply,
    audit entry names it explicitly."""
    sent: dict = {}

    def _fake_send(**kwargs):
        sent.update(kwargs)
        return "<reply@x>"

    monkeypatch.setattr(inbox_hook, "send_reply", _fake_send)
    monkeypatch.setattr(
        inbox_hook,
        "get_object",
        lambda *a, **kw: pytest.fail("get_object must not run when blob key is absent"),
    )

    dec = _decision(outcome="approve", rationale="You're eligible.")
    event = _event(
        decision_dict=dec.model_dump(mode="json"),
        reply_attachment_blob_key=None,
    )
    reply_id = _send_pap_reply_for_event(MagicMock(), event, _settings())
    assert reply_id == "<reply@x>"
    assert sent["attachments"] is None
    audit_notes = [e["note"] for e in event.review_history]
    assert any("text-only" in note for note in audit_notes)


def test_send_reply_attaches_pdf_when_blob_fetch_succeeds(monkeypatch):
    """Happy path — PDF pulled from blob store, attached to the reply."""
    sent: dict = {}

    def _fake_send(**kwargs):
        sent.update(kwargs)
        return "<reply@x>"

    monkeypatch.setattr(inbox_hook, "send_reply", _fake_send)
    monkeypatch.setattr(inbox_hook, "get_object", lambda bucket, key: b"PDFBYTES")

    dec = _decision(outcome="approve")
    event = _event(
        decision_dict=dec.model_dump(mode="json"),
        reply_attachment_blob_key="inbox-replies/e1/decision.pdf",
    )
    _send_pap_reply_for_event(MagicMock(), event, _settings())
    assert sent["attachments"] is not None
    assert len(sent["attachments"]) == 1
    attachment = sent["attachments"][0]
    assert attachment.filename == "decision.pdf"
    assert attachment.content == b"PDFBYTES"


def test_send_reply_degrades_to_text_when_blob_fetch_fails(monkeypatch):
    """Blob store outage on a case with a pre-rendered PDF: don't block
    the applicant — send a text-only reply, log the fetch failure. The
    approved send is more important than the PDF reproduction on the
    reviewer's page."""
    sent: dict = {}

    def _fake_send(**kwargs):
        sent.update(kwargs)
        return "<reply@x>"

    def _boom(*a, **kw):
        raise RuntimeError("s3 timeout")

    monkeypatch.setattr(inbox_hook, "send_reply", _fake_send)
    monkeypatch.setattr(inbox_hook, "get_object", _boom)

    dec = _decision(outcome="approve")
    event = _event(
        decision_dict=dec.model_dump(mode="json"),
        reply_attachment_blob_key="inbox-replies/e1/decision.pdf",
    )
    reply_id = _send_pap_reply_for_event(MagicMock(), event, _settings())
    assert reply_id == "<reply@x>"
    assert sent["attachments"] is None
    audit_notes = [e["note"] for e in event.review_history]
    assert any("text-only" in note for note in audit_notes)


def test_send_reply_writes_reply_sent_audit(monkeypatch):
    """Every successful send emits a `reply_sent` audit entry for the
    reviewer timeline — this is the moment the applicant hears from us."""
    monkeypatch.setattr(inbox_hook, "send_reply", lambda **kw: "<reply@x>")
    monkeypatch.setattr(inbox_hook, "get_object", lambda *a, **kw: b"PDF")

    dec = _decision(outcome="deny", rationale="Income exceeds threshold.")
    event = _event(
        from_addr="pat@example.com",
        decision_dict=dec.model_dump(mode="json"),
        reply_attachment_blob_key="inbox-replies/e1/deny.pdf",
    )
    _send_pap_reply_for_event(MagicMock(), event, _settings())
    reply_sent = [e for e in event.review_history if e.get("action") == "reply_sent"]
    assert len(reply_sent) == 1
    assert "pat@example.com" in reply_sent[0]["note"]
    assert "deny" in reply_sent[0]["note"]


# Guard the module also uses the assembler-error catch surface — the base
# class covers subclasses (AssemblerRenderFailed, AssemblerItemMissing).
def test_assembler_error_subclass_hierarchy():
    """Regression guard on the exception hierarchy — the fallback branch
    catches AssemblerError, so future subclasses inherit the same
    degrade-to-legacy behavior. If someone re-parents these classes,
    the fallback stops firing and this test flags it."""
    assert issubclass(AssemblerRenderFailed, AssemblerError)
    assert issubclass(AssemblerMissingBinding, AssemblerError)
