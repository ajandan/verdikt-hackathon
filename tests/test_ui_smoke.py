"""UI smoke tests — verify Jinja templates render and routes are wired.

These do not exercise the DB / blob layer. They cover only the routes that render
static forms with no session dependency; deeper UI tests belong behind a Postgres
+ MinIO test fixture, which we do not yet spin up in CI.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from docgen.auth import sign_session
from docgen.main import app


def _admin_client() -> TestClient:
    """A TestClient with a valid admin session cookie set."""
    client = TestClient(app)
    client.cookies.set("docgen_session", sign_session("admin"))
    return client


def test_root_serves_landing_and_authenticated_redirect() -> None:
    # One TestClient covers both cases so the MCP session manager (mounted
    # at /mcp/) only sees a single lifespan startup — see also `_admin_client`.
    client = TestClient(app, follow_redirects=False)

    # Anonymous → landing HTML with the headline + login CTA.
    r_anon = client.get("/")
    assert r_anon.status_code == 200
    assert "text/html" in r_anon.headers["content-type"]
    assert "Design-time AI" in r_anon.text
    assert "/ui/login" in r_anon.text

    # Authenticated → straight to the dashboard.
    client.cookies.set("docgen_session", sign_session("admin"))
    r_auth = client.get("/")
    assert r_auth.status_code == 307
    assert r_auth.headers["location"] == "/ui/"


def test_upload_form_renders_for_admin() -> None:
    with _admin_client() as client:
        r = client.get("/ui/upload")
    assert r.status_code == 200
    assert "<form" in r.text
    assert "Upload template" in r.text
    assert 'enctype="multipart/form-data"' in r.text


def test_upload_form_requires_admin_session() -> None:
    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/ui/upload")
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


def test_login_form_renders() -> None:
    with TestClient(app) as client:
        r = client.get("/ui/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


def test_health_still_works() -> None:
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_request_id_header_echoed() -> None:
    with TestClient(app) as client:
        r = client.get("/health", headers={"x-request-id": "test-req-xyz"})
    assert r.headers.get("x-request-id") == "test-req-xyz"


def test_diff_route_404s_on_unknown_template() -> None:
    import uuid as _u

    with _admin_client() as client:
        r = client.get(f"/ui/templates/{_u.uuid4()}/diff")
    assert r.status_code == 404


def test_dashboard_renders_all_sections() -> None:
    """Dashboard shows stats, sparkline, recent activity, failures, top templates."""
    with _admin_client() as client:
        r = client.get("/ui/dashboard")
    assert r.status_code == 200
    body = r.text
    assert "Dashboard" in body
    # Stat tiles (shared with index)
    assert "Pending drafts" in body
    assert "Renders (24h)" in body
    assert "Warnings" in body
    # Dashboard-specific sections
    assert "Recent renders" in body
    assert "Recent publishes" in body
    assert "Failing jobs" in body
    assert "Top templates" in body


def test_dashboard_requires_admin_session() -> None:
    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/ui/dashboard")
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


def test_index_renders_stat_cards_and_toolbar() -> None:
    """Landing page shows the 4-card stats strip + search/status toolbar.

    Runs against an empty (or small) tenant set — we only assert scaffolding
    is present, not specific counts, so this doesn't depend on seed data.
    """
    with _admin_client() as client:
        r = client.get("/ui/")
    assert r.status_code == 200
    body = r.text
    # Stat cards
    assert "stats-grid" in body
    assert "Templates" in body
    assert "Pending drafts" in body
    assert "Renders (24h)" in body
    assert "Warnings" in body
    # Toolbar only renders when there are template rows; scaffolding CSS
    # is always inlined so we assert on class definitions instead.
    assert ".stat-card" in body
    assert ".chip" in body


# ---------------------------------------------------------------------------
# Schema viewer
# ---------------------------------------------------------------------------

def test_schema_page_requires_admin_session() -> None:
    import uuid as _u

    with TestClient(app, follow_redirects=False) as client:
        r = client.get(f"/ui/templates/{_u.uuid4()}/schema")
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


def test_schema_page_404_on_unknown_template() -> None:
    import uuid as _u

    with _admin_client() as client:
        r = client.get(f"/ui/templates/{_u.uuid4()}/schema")
    assert r.status_code == 404


def test_schema_page_renders_fields_and_copy_button(default_tenant) -> None:
    """Seed a frozen version with a known 3-field schema, assert the view
    lists each field, shows types, and includes the copy-JSON scaffolding."""
    import uuid as _u

    from docgen.store.db import sync_session
    from docgen.store.models import Template, TemplateVersion

    tid = _u.uuid4()
    with sync_session() as s:
        tmpl = Template(id=tid, name=f"schema-ui-{tid.hex[:6]}", tenant_id=default_tenant)
        s.add(tmpl)
        s.flush()
        tv = TemplateVersion(
            template_id=tid,
            version=1,
            locale="default",
            schema={"fields": [
                {"name": "Patient_First_Name__c", "type": "string", "required": True},
                {"name": "Household_Size__c", "type": "integer", "required": True},
                {"name": "Proof_Pay_Stubs__c", "type": "boolean", "required": False},
            ]},
            preflight={"outcome": "accept", "findings": [], "detected_variables": []},
            blob_key=f"frozen/{tid}/v1/default.docx",
            version_hash="ab" * 32,
            status="frozen",
            is_frozen=True,
        )
        s.add(tv)
        s.commit()

    try:
        with _admin_client() as client:
            # Explicit ?version=1 — we didn't publish, so current_version is None.
            r = client.get(f"/ui/templates/{tid}/schema?version=1")
        assert r.status_code == 200, r.text[:400]
        body = r.text
        # Each field name is rendered.
        assert "Patient_First_Name__c" in body
        assert "Household_Size__c" in body
        assert "Proof_Pay_Stubs__c" in body
        # Type labels appear.
        assert "string" in body and "integer" in body and "boolean" in body
        # Optional badge for the non-required field.
        assert "optional" in body
        # Copy-JSON affordance and the raw JSON.
        assert "Copy JSON" in body
        assert "schema-json-src" in body
    finally:
        with sync_session() as s:
            t = s.get(Template, tid)
            if t is not None:
                s.delete(t)
                s.commit()
