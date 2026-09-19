"""UI write-paths for the tenant/program models + Triage-routing pages.

Covers the four POST handlers introduced with the 3-level config chain
and the "Inherit from tenant" toggles:

  * POST /ui/programs/{id}/models         — inherit_<field> clears the
    program column so the resolver falls back to tenant/platform.
  * POST /ui/programs/{id}/triage-routing — inherit_routing=1 clears
    program.triage_routing entirely. Without it, only rows that differ
    from the tenant/platform baseline persist.
  * POST /ui/admin/tenants/{id}/models    — blank field clears the
    tenant column; explicit value overrides the platform default.
  * POST /ui/admin/tenants/{id}/triage-routing — dedicated route for
    the split-out Triage page; rows equal to the platform default are
    dropped so JSONB stays audit-visible.

These are full-stack tests: real DB writes, real router, real CSRF
middleware. Tests seed then tear down their own tenant + program so
they don't collide with the shared fixture data.
"""
from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import delete

from docgen.auth import CSRF_COOKIE, SESSION_COOKIE, sign_session
from docgen.main import app
from docgen.store.db import sync_session
from docgen.store.models import Program, Tenant

CSRF_TOKEN = "test-inherit-csrf"


def _admin_client() -> TestClient:
    """TestClient with super_admin session + CSRF cookies set."""
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, sign_session("admin"))
    client.cookies.set(CSRF_COOKIE, CSRF_TOKEN)
    return client


def _seed_tenant_program(
    *,
    program_extract: str | None = None,
    program_reason: str | None = None,
    program_triage: str | None = None,
    program_verifier: str | None = None,
    program_triage_routing: dict | None = None,
    tenant_extract: str | None = None,
    tenant_triage: str | None = None,
    tenant_triage_routing: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one tenant + one program with the given override state."""
    tid = uuid.uuid4()
    pid = uuid.uuid4()
    with sync_session() as s:
        s.add(Tenant(
            id=tid,
            name=f"inherit-t-{tid.hex[:6]}",
            extract_model=tenant_extract,
            triage_model=tenant_triage,
            triage_routing=tenant_triage_routing,
        ))
        s.flush()
        s.add(Program(
            id=pid, tenant_id=tid,
            slug=f"p-{tid.hex[:6]}",
            name=f"program-{tid.hex[:6]}",
            extract_model=program_extract,
            reason_model=program_reason,
            triage_model=program_triage,
            verifier_extract_model=program_verifier,
            triage_routing=program_triage_routing,
            extraction_schema=[],
            history_slots=[],
        ))
        s.commit()
    return tid, pid


def _teardown(tid: uuid.UUID) -> None:
    with sync_session() as s:
        s.execute(delete(Program).where(Program.tenant_id == tid))
        s.execute(delete(Tenant).where(Tenant.id == tid))
        s.commit()


def _reload_program(pid: uuid.UUID) -> Program:
    with sync_session() as s:
        return s.get(Program, pid)


def _reload_tenant(tid: uuid.UUID) -> Tenant:
    with sync_session() as s:
        return s.get(Tenant, tid)


# ---------------------------------------------------------------------------
# Program models — inherit checkbox per field
# ---------------------------------------------------------------------------

def test_program_models_inherit_checkbox_clears_all_four_fields() -> None:
    """Every model field submitted with its inherit_<field>=1 checkbox
    ticked must land as NULL, regardless of what the (disabled) input
    still submitted."""
    tid, pid = _seed_tenant_program(
        program_extract="google/gemma-3-27b-it",
        program_reason="nvidia/Nemotron-3-Ultra-550b-a55b",
        program_triage="nvidia/nemotron-nano-3-6b",
        program_verifier="google/gemma-3-27b-it",
    )
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/programs/{pid}/models",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "inherit_extract_model": "1",
                    "inherit_reason_model": "1",
                    "inherit_triage_model": "1",
                    "inherit_verifier_extract_model": "1",
                    # These values must be IGNORED because inherit is checked.
                    "extract_model": "google/gemma-3-27b-it",
                    "reason_model": "nvidia/Nemotron-3-Ultra-550b-a55b",
                    "triage_model": "nvidia/nemotron-nano-3-6b",
                    "verifier_extract_model": "google/gemma-3-27b-it",
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        prog = _reload_program(pid)
        assert prog.extract_model is None
        assert prog.reason_model is None
        assert prog.triage_model is None
        assert prog.verifier_extract_model is None
    finally:
        _teardown(tid)


def test_program_models_saves_value_when_inherit_unchecked() -> None:
    """No inherit_<field> in the form = the operator wants to override.
    The submitted dropdown value persists on the program row."""
    tid, pid = _seed_tenant_program()  # everything NULL initially
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/programs/{pid}/models",
                data={
                    "csrf_token": CSRF_TOKEN,
                    # No inherit_* keys — the operator unchecked all four boxes.
                    "extract_model": "google/gemma-3-27b-it",
                    "reason_model": "nvidia/Nemotron-3-Ultra-550b-a55b",
                    "triage_model": "nvidia/nemotron-nano-3-6b",
                    "verifier_extract_model": "google/gemma-3-27b-it",
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        prog = _reload_program(pid)
        assert prog.extract_model == "google/gemma-3-27b-it"
        assert prog.reason_model == "nvidia/Nemotron-3-Ultra-550b-a55b"
        assert prog.triage_model == "nvidia/nemotron-nano-3-6b"
        assert prog.verifier_extract_model == "google/gemma-3-27b-it"
    finally:
        _teardown(tid)


def test_program_models_inherit_is_per_field_not_all_or_nothing() -> None:
    """Mixed state: inherit extract + verifier, override reason + triage.
    Regression guard for a naive `if inherit_any: clear all` implementation."""
    tid, pid = _seed_tenant_program(
        program_extract="google/gemma-3-27b-it",
        program_verifier="google/gemma-3-27b-it",
    )
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/programs/{pid}/models",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "inherit_extract_model": "1",
                    "inherit_verifier_extract_model": "1",
                    # reason + triage NOT inheriting → values persist.
                    "reason_model": "nvidia/Nemotron-3-Ultra-550b-a55b",
                    "triage_model": "nvidia/nemotron-nano-3-6b",
                    # Ignored (inherit ticked):
                    "extract_model": "google/gemma-3-27b-it",
                    "verifier_extract_model": "google/gemma-3-27b-it",
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        prog = _reload_program(pid)
        assert prog.extract_model is None
        assert prog.verifier_extract_model is None
        assert prog.reason_model == "nvidia/Nemotron-3-Ultra-550b-a55b"
        assert prog.triage_model == "nvidia/nemotron-nano-3-6b"
    finally:
        _teardown(tid)


# ---------------------------------------------------------------------------
# Program triage routing — inherit_routing toggle
# ---------------------------------------------------------------------------

def test_program_triage_routing_inherit_clears_program_overrides() -> None:
    """inherit_routing=1 on the form must wipe program.triage_routing so
    the resolver falls back to tenant/platform routing entirely."""
    tid, pid = _seed_tenant_program(
        program_triage_routing={
            "refill": {"route": "reject", "decision_needed": True},
        },
    )
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/programs/{pid}/triage-routing",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "inherit_routing": "1",
                    # An accidental payload MUST be ignored when inheriting.
                    "routing_json": json.dumps([
                        {"case_type": "refill", "route": "reject",
                         "decision_needed": True},
                    ]),
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        prog = _reload_program(pid)
        assert prog.triage_routing is None
    finally:
        _teardown(tid)


def test_program_triage_routing_without_inherit_stores_diff_only() -> None:
    """No inherit_routing on the form + a row that differs from the
    tenant/platform baseline must persist. Rows that match the baseline
    are dropped so the stored JSONB clearly shows program overrides."""
    tid, pid = _seed_tenant_program()  # tenant has no overrides
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/programs/{pid}/triage-routing",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "routing_json": json.dumps([
                        # Matches platform default → dropped.
                        {"case_type": "new_intake", "route": "decide",
                         "decision_needed": True},
                        # Differs → kept.
                        {"case_type": "refill", "route": "reject",
                         "decision_needed": True},
                    ]),
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        prog = _reload_program(pid)
        assert prog.triage_routing == {
            "refill": {"route": "reject", "decision_needed": True},
        }
    finally:
        _teardown(tid)


def test_program_triage_routing_empty_payload_clears_overrides() -> None:
    """Empty routing_json (no rows) with no inherit toggle still clears
    the program overrides — the operator removed every row on purpose."""
    tid, pid = _seed_tenant_program(
        program_triage_routing={
            "refill": {"route": "reject", "decision_needed": True},
        },
    )
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/programs/{pid}/triage-routing",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "routing_json": "[]",
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        prog = _reload_program(pid)
        assert prog.triage_routing is None
    finally:
        _teardown(tid)


# ---------------------------------------------------------------------------
# Tenant models — split page (models only, routing has its own page)
# ---------------------------------------------------------------------------

def test_tenant_models_saves_dropdown_values_and_clears_blanks() -> None:
    """POST /ui/admin/tenants/{id}/models writes the 5 model columns.
    A blank value in the "— use platform default —" option clears
    to NULL so env-var rotation propagates."""
    tid, _ = _seed_tenant_program(
        tenant_extract="pre-existing-extract",
        tenant_triage="pre-existing-triage",
    )
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/admin/tenants/{tid}/models",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "extract_model": "google/gemma-3-27b-it",
                    "triage_model": "",  # blank → NULL
                    "retrieve_model": "",
                    "reason_model": "nvidia/Nemotron-3-Ultra-550b-a55b",
                    "verifier_model": "",
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        tenant = _reload_tenant(tid)
        assert tenant.extract_model == "google/gemma-3-27b-it"
        assert tenant.triage_model is None
        assert tenant.retrieve_model is None
        assert tenant.reason_model == "nvidia/Nemotron-3-Ultra-550b-a55b"
        assert tenant.verifier_model is None
    finally:
        _teardown(tid)


def test_tenant_models_page_does_not_touch_triage_routing() -> None:
    """After the Triage page was split off, saving Models must leave
    tenant.triage_routing exactly as it was — no accidental clobber."""
    seed_routing = {
        "refill": {"route": "crm_outreach", "decision_needed": False},
    }
    tid, _ = _seed_tenant_program(tenant_triage_routing=seed_routing)
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/admin/tenants/{tid}/models",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "extract_model": "google/gemma-3-27b-it",
                    "triage_model": "",
                    "retrieve_model": "",
                    "reason_model": "",
                    "verifier_model": "",
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        tenant = _reload_tenant(tid)
        assert tenant.triage_routing == seed_routing
    finally:
        _teardown(tid)


# ---------------------------------------------------------------------------
# Tenant triage routing — dedicated page
# ---------------------------------------------------------------------------

def test_tenant_triage_routing_stores_only_platform_default_diffs() -> None:
    """Rows that equal the platform default must not persist; keeps the
    JSONB minimal and makes tenant overrides audit-visible."""
    tid, _ = _seed_tenant_program()
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/admin/tenants/{tid}/triage-routing",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "routing_json": json.dumps([
                        # Matches platform default (new_intake → decide) → drop.
                        {"case_type": "new_intake", "route": "decide",
                         "decision_needed": True},
                        # Differs → keep.
                        {"case_type": "refill", "route": "crm_outreach",
                         "decision_needed": False},
                    ]),
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        tenant = _reload_tenant(tid)
        assert tenant.triage_routing == {
            "refill": {"route": "crm_outreach", "decision_needed": False},
        }
    finally:
        _teardown(tid)


def test_tenant_triage_routing_all_defaults_clears_column() -> None:
    """Submitting only rows that already match the platform default must
    end up with tenant.triage_routing = NULL (nothing to store)."""
    tid, _ = _seed_tenant_program(
        tenant_triage_routing={
            "refill": {"route": "crm_outreach", "decision_needed": False},
        },
    )
    try:
        with _admin_client() as client:
            r = client.post(
                f"/ui/admin/tenants/{tid}/triage-routing",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "routing_json": json.dumps([
                        {"case_type": "new_intake", "route": "decide",
                         "decision_needed": True},
                        {"case_type": "refill", "route": "pharmacy_direct",
                         "decision_needed": False},
                    ]),
                },
            )
        assert r.status_code in (200, 302, 303), r.text[:400]
        tenant = _reload_tenant(tid)
        assert tenant.triage_routing is None
    finally:
        _teardown(tid)


def test_tenant_triage_routing_page_renders_with_seeded_rows() -> None:
    """GET must render the split-out page with the resolved (tenant +
    platform defaults, merged) routing rows visible in the HTML."""
    tid, _ = _seed_tenant_program(
        tenant_triage_routing={
            "refill": {"route": "crm_outreach", "decision_needed": False},
        },
    )
    try:
        with _admin_client() as client:
            r = client.get(f"/ui/admin/tenants/{tid}/triage-routing")
        assert r.status_code == 200, r.text[:400]
        body = r.text
        assert "Triage routing" in body
        # Case types from the platform default are all present.
        assert "new_intake" in body
        assert "refill" in body
        assert "appeal" in body
        # Tenant override applied to refill row.
        assert "crm_outreach" in body
    finally:
        _teardown(tid)
