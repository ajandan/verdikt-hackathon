"""Rule authoring vocabulary validation.

Locks the contract that:

* GET ``/ui/programs/{id}/slots.json`` returns the program's declared
  slot names — the source of truth for the client-side ``@`` typeahead
  and the Intake-fields datalist.
* POST ``/ui/rules/{id}`` validates every Intake fields[] entry AND
  every ``@field`` in criterion/exception against the program's schema.
  On error, the response is a 400 with the rendered form inline
  (option-b UX: preserve the operator's in-flight edits, mark the
  offending inputs with per-input error messages), not a redirect
  with a flash.

These are the write-path guardrails that make the client widgets
trustworthy — without server enforcement, a curl POST could still land
a rule whose ``@fieldname`` refs point at slots that don't exist.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from docgen.auth import CSRF_COOKIE, SESSION_COOKIE, sign_session
from docgen.main import app
from docgen.store.db import sync_session
from docgen.store.models import Program, Rule, RuleVersion, Tenant

CSRF = "test-edit-vocab-csrf"


def _admin_client() -> TestClient:
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(SESSION_COOKIE, sign_session("admin"))
    c.cookies.set(CSRF_COOKIE, CSRF)
    return c


def _seed(*, criterion: str = "@ins_uninsured is true", program_scoped: bool = True) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid = uuid.uuid4()
    pid = uuid.uuid4()
    rid = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=tid, name=f"vocab-t-{tid.hex[:6]}"))
        s.flush()
        s.add(Program(
            id=pid, tenant_id=tid,
            slug=f"vocab-p-{tid.hex[:6]}",
            name=f"program-{tid.hex[:6]}",
            extraction_schema=[
                {"name": "ins_uninsured", "type": "boolean", "description": "true if uninsured"},
                {"name": "rx_medication", "type": "string", "description": "drug name"},
                {"name": "patient_zip", "type": "string", "description": "ZIP"},
            ],
            history_slots=[],
        ))
        s.add(Rule(
            id=rid, tenant_id=tid,
            program_id=pid if program_scoped else None,
            rule_id=f"VOCAB-{tid.hex[:4].upper()}",
            kind="insurance",
            title="Vocab test rule",
            criterion=criterion,
            exception="",
            fields=["ins_uninsured"] if program_scoped else [],
            field_refs=[],
            is_active=True,
        ))
        s.commit()
    return tid, pid, rid


def _teardown(tid: uuid.UUID) -> None:
    with sync_session() as s:
        s.execute(delete(RuleVersion).where(
            RuleVersion.rule_id.in_(
                s.query(Rule.id).filter(Rule.tenant_id == tid)
            )
        ))
        s.execute(delete(Rule).where(Rule.tenant_id == tid))
        s.execute(delete(Program).where(Program.tenant_id == tid))
        s.execute(delete(Tenant).where(Tenant.id == tid))
        s.commit()


# ---------------------------------------------------------------------------
# GET /ui/programs/{id}/slots.json
# ---------------------------------------------------------------------------

def test_slots_json_returns_declared_slot_names() -> None:
    """The endpoint the client-side typeahead + datalist fetch from."""
    tid, pid, _ = _seed()
    try:
        with _admin_client() as c:
            r = c.get(f"/ui/programs/{pid}/slots.json")
        assert r.status_code == 200
        payload = r.json()
        assert payload["program_id"] == str(pid)
        names = [s["name"] for s in payload["slots"]]
        assert set(names) == {"ins_uninsured", "rx_medication", "patient_zip"}
        # Each slot carries type + description so the popover can render
        # a richer picker without a second fetch.
        for slot in payload["slots"]:
            assert "type" in slot and "description" in slot
    finally:
        _teardown(tid)


def test_slots_json_404_when_program_missing() -> None:
    """Non-existent program id → 404 like every other /ui/programs/{id}/… route."""
    with _admin_client() as c:
        r = c.get(f"/ui/programs/{uuid.uuid4()}/slots.json")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /ui/rules/{id} — save-time validation
# ---------------------------------------------------------------------------

def test_save_happy_path_persists_and_redirects() -> None:
    """Every field valid → normal 303 redirect + row updated + version
    snapshot. Regression guard for the same MissingGreenlet trap the
    copy-to-program handler had — locals cached before commit."""
    tid, _pid, rid = _seed(criterion="@ins_uninsured is true")
    try:
        with _admin_client() as c:
            r = c.post(
                f"/ui/rules/{rid}",
                data={
                    "csrf_token": CSRF,
                    "kind": "insurance",
                    "title": "Vocab test rule",
                    "fields": "ins_uninsured",
                    "criterion": "@ins_uninsured is true",
                    "exception": "",
                    "is_active": "on",
                    # No AI creds in test env — the design-time translator
                    # would fail-closed and block the save. Skip it here;
                    # the translator + gate have their own tests.
                    "skip_translator": "on",
                },
            )
        assert r.status_code in (302, 303), r.text[:400]
        with sync_session() as s:
            rule = s.get(Rule, rid)
            assert rule.criterion == "@ins_uninsured is true"
            assert set(rule.field_refs) == {"ins_uninsured"}
    finally:
        _teardown(tid)


def test_save_unknown_intake_field_renders_inline_error() -> None:
    """Fields entry outside the schema → 400 with the form re-rendered
    and a .field-error block under the fields input. Operator's
    in-flight values (criterion/exception/title) are preserved."""
    tid, _pid, rid = _seed()
    try:
        with _admin_client() as c:
            r = c.post(
                f"/ui/rules/{rid}",
                data={
                    "csrf_token": CSRF,
                    "kind": "insurance",
                    "title": "Vocab test rule",
                    "fields": "ins_uninsured, not_a_real_slot",
                    "criterion": "@ins_uninsured is true",
                    "exception": "",
                    "is_active": "on",
                },
            )
        assert r.status_code == 400, r.text[:400]
        body = r.text
        # Inline error markup rendered under the fields input.
        assert "field-error" in body
        assert "not_a_real_slot" in body
        # DB unchanged.
        with sync_session() as s:
            rule = s.get(Rule, rid)
            assert "not_a_real_slot" not in (rule.fields or [])
    finally:
        _teardown(tid)


def test_save_unknown_at_field_ref_in_criterion_renders_inline_error() -> None:
    """@field in criterion that isn't a declared slot → inline error
    attributed to criterion + "did you mean" suggestion via
    validate_field_refs."""
    tid, _pid, rid = _seed()
    try:
        with _admin_client() as c:
            r = c.post(
                f"/ui/rules/{rid}",
                data={
                    "csrf_token": CSRF,
                    "kind": "insurance",
                    "title": "Vocab test rule",
                    "fields": "ins_uninsured",
                    "criterion": "@ins_uninsurd is true",  # typo
                    "exception": "",
                    "is_active": "on",
                },
            )
        assert r.status_code == 400
        body = r.text
        assert "field-error" in body
        assert "@ins_uninsurd" in body
        # DB unchanged — criterion still the original.
        with sync_session() as s:
            rule = s.get(Rule, rid)
            assert "@ins_uninsurd" not in rule.criterion
    finally:
        _teardown(tid)


def test_save_unknown_at_field_ref_in_exception_renders_inline_error() -> None:
    """Exception-only error attributes to the exception input, not criterion."""
    tid, _pid, rid = _seed()
    try:
        with _admin_client() as c:
            r = c.post(
                f"/ui/rules/{rid}",
                data={
                    "csrf_token": CSRF,
                    "kind": "insurance",
                    "title": "Vocab test rule",
                    "fields": "ins_uninsured",
                    "criterion": "@ins_uninsured is true",
                    "exception": "skip when @never_declared",
                    "is_active": "on",
                },
            )
        assert r.status_code == 400
        assert "@never_declared" in r.text
    finally:
        _teardown(tid)


def test_library_rule_without_program_skips_vocab_validation() -> None:
    """Library rule with no attached program → no schema to validate
    against, so any names pass. Copy-to-program wizard is the path
    where vocabulary gets bound. Regression guard against making the
    validation gate accidentally apply to library rules."""
    tid, _pid, rid = _seed(criterion="anything goes here", program_scoped=False)
    try:
        with _admin_client() as c:
            r = c.post(
                f"/ui/rules/{rid}",
                data={
                    "csrf_token": CSRF,
                    "kind": "insurance",
                    "title": "Free-form library rule",
                    "fields": "made_up_field, another_one",
                    "criterion": "@made_up ok because no schema is bound",
                    "exception": "",
                    "is_active": "on",
                    "skip_translator": "on",
                },
            )
        # Happy 303 — no vocabulary check applied.
        assert r.status_code in (302, 303), r.text[:400]
    finally:
        _teardown(tid)
