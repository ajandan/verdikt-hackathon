"""Copy-to-program wizard end-to-end.

Drives the full flow through TestClient:

1. Seed a tenant + a program (with a form-native extraction_schema) + a
   library rule whose criterion references canonical @field names.
2. GET the wizard with a target program → assert the review table
   renders the auto-mapped rows.
3. POST the mapping → assert a new program-scoped rules row exists with
   the criterion / exception / fields rewritten to the target vocabulary.

Locks the invariants operators rely on: library rule stays untouched,
new rule carries program_id + rewritten refs, snapshot version recorded.
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


CSRF = "test-wizard-csrf"


def _admin_client() -> TestClient:
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(SESSION_COOKIE, sign_session("admin"))
    c.cookies.set(CSRF_COOKIE, CSRF)
    return c


def _seed_tenant_program_and_library_rule(
    program_schema: list[dict],
    criterion: str,
    exception: str = "",
    fields: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid = uuid.uuid4()
    pid = uuid.uuid4()
    rid = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(id=tid, name=f"wiz-t-{tid.hex[:6]}"))
        s.flush()
        s.add(Program(
            id=pid, tenant_id=tid,
            slug=f"wiz-p-{tid.hex[:6]}",
            name=f"program-{tid.hex[:6]}",
            extraction_schema=program_schema,
            history_slots=[],
        ))
        # Library rule (program_id NULL) with @field references in the
        # criterion — the whole point of the wizard is to rewrite these.
        s.add(Rule(
            id=rid, tenant_id=tid,
            program_id=None,
            rule_id=f"WIZ-TEST-{tid.hex[:4].upper()}",
            kind="insurance",
            title="Test rule",
            criterion=criterion,
            exception=exception,
            fields=fields or [],
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
# GET the wizard
# ---------------------------------------------------------------------------

def test_wizard_get_without_target_shows_program_dropdown() -> None:
    """Step 1: no target selected — page renders with a program dropdown
    scoped to the rule's tenant."""
    tid, pid, rid = _seed_tenant_program_and_library_rule(
        program_schema=[
            {"name": "ins_uninsured", "type": "boolean"},
            {"name": "rx_medication", "type": "string"},
        ],
        criterion="@insurance_status is uninsured",
    )
    try:
        with _admin_client() as c:
            r = c.get(f"/ui/rules/{rid}/copy-to-program")
        assert r.status_code == 200, r.text[:400]
        body = r.text
        assert "Choose the target program" in body
        # The seeded program appears in the dropdown
        assert f"wiz-p-{tid.hex[:6]}" in body
    finally:
        _teardown(tid)


def test_wizard_get_with_target_renders_auto_mapping_table() -> None:
    """Step 2: target selected — auto-mapping runs and renders a row per
    @field ref in the source rule, with an aliased suggestion where the
    curated alias table has one (insurance_status ↔ ins_uninsured)."""
    tid, pid, rid = _seed_tenant_program_and_library_rule(
        program_schema=[
            {"name": "ins_uninsured", "type": "boolean"},
            {"name": "rx_medication", "type": "string"},
            {"name": "patient_zip", "type": "string"},
        ],
        criterion="@insurance_status is uninsured AND @prescribed_drug",
        exception="unless @postal_code is empty",
    )
    try:
        with _admin_client() as c:
            r = c.get(f"/ui/rules/{rid}/copy-to-program?target={pid}")
        assert r.status_code == 200, r.text[:400]
        body = r.text
        # All three source refs rendered as rows.
        for src_ref in ("@insurance_status", "@prescribed_drug", "@postal_code"):
            assert src_ref in body, f"missing {src_ref} in wizard body"
        # The confidence pill classes are present for the mapped rows.
        assert "conf-" in body
    finally:
        _teardown(tid)


# ---------------------------------------------------------------------------
# POST the mapping — the wire-through
# ---------------------------------------------------------------------------

def test_wizard_post_creates_program_scoped_copy_with_rewritten_refs() -> None:
    """Full round-trip: submit the mapping, verify a new program-scoped
    Rule row exists with criterion + exception rewritten to the target
    slot names and a RuleVersion snapshot recorded."""
    tid, pid, rid = _seed_tenant_program_and_library_rule(
        program_schema=[
            {"name": "ins_uninsured", "type": "boolean"},
            {"name": "rx_medication", "type": "string"},
            {"name": "patient_zip", "type": "string"},
        ],
        criterion="@insurance_status is uninsured AND @prescribed_drug is Cardiovita",
        exception="skip if @postal_code is empty",
        fields=["insurance_status", "prescribed_drug", "postal_code"],
    )
    try:
        with _admin_client() as c:
            r = c.post(
                f"/ui/rules/{rid}/copy-to-program",
                data={
                    "csrf_token": CSRF,
                    "target_program_id": str(pid),
                    "map_insurance_status": "ins_uninsured",
                    "map_prescribed_drug": "rx_medication",
                    "map_postal_code": "patient_zip",
                },
            )
        assert r.status_code in (302, 303), r.text[:400]

        with sync_session() as s:
            # Library rule untouched.
            source = s.get(Rule, rid)
            assert source is not None
            assert source.program_id is None
            assert "@insurance_status" in source.criterion

            # New program-scoped copy exists with rewritten text.
            copies = (
                s.query(Rule)
                .filter(Rule.tenant_id == tid, Rule.program_id == pid)
                .all()
            )
            assert len(copies) == 1
            copied = copies[0]
            assert "@ins_uninsured" in copied.criterion
            assert "@rx_medication" in copied.criterion
            assert "@insurance_status" not in copied.criterion
            assert "@patient_zip" in copied.exception
            assert set(copied.fields) == {"ins_uninsured", "rx_medication", "patient_zip"}
            assert set(copied.field_refs) >= {"ins_uninsured", "rx_medication", "patient_zip"}

            # Version snapshot recorded so the audit trail carries the
            # copy event.
            versions = (
                s.query(RuleVersion)
                .filter(RuleVersion.rule_id == copied.id)
                .all()
            )
            assert len(versions) == 1
    finally:
        _teardown(tid)


def test_wizard_post_rejects_unmapped_refs() -> None:
    """Missing map_<source> entry → redirect back with an error flash;
    no new rule is created."""
    tid, pid, rid = _seed_tenant_program_and_library_rule(
        program_schema=[{"name": "ins_uninsured", "type": "boolean"}],
        criterion="@insurance_status AND @drug_name",
    )
    try:
        with _admin_client() as c:
            r = c.post(
                f"/ui/rules/{rid}/copy-to-program",
                data={
                    "csrf_token": CSRF,
                    "target_program_id": str(pid),
                    # Deliberately don't include map_drug_name
                    "map_insurance_status": "ins_uninsured",
                },
            )
        assert r.status_code in (302, 303)
        with sync_session() as s:
            copies = (
                s.query(Rule)
                .filter(Rule.tenant_id == tid, Rule.program_id == pid)
                .all()
            )
            assert copies == []
    finally:
        _teardown(tid)


def test_wizard_post_rejects_target_slot_not_on_schema() -> None:
    """Post-substitution validation catches a hand-picked value that
    isn't declared on the target program — defense-in-depth even though
    the wizard's UI only offers declared slots in the dropdown."""
    tid, pid, rid = _seed_tenant_program_and_library_rule(
        program_schema=[{"name": "ins_uninsured", "type": "boolean"}],
        criterion="@insurance_status",
    )
    try:
        with _admin_client() as c:
            r = c.post(
                f"/ui/rules/{rid}/copy-to-program",
                data={
                    "csrf_token": CSRF,
                    "target_program_id": str(pid),
                    "map_insurance_status": "not_on_this_program",
                },
            )
        assert r.status_code in (302, 303)
        with sync_session() as s:
            copies = (
                s.query(Rule)
                .filter(Rule.tenant_id == tid, Rule.program_id == pid)
                .all()
            )
            assert copies == []
    finally:
        _teardown(tid)
