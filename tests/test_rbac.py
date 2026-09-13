"""RBAC: super_admin vs tenant_admin scoping in the UI.

Covers:
  * super_admin-only routes reject tenant_admin with 404 (not 403 — we
    don't disclose whether the URL exists to lower-privilege users).
  * tenant_admin cannot access another tenant's template detail.
  * tenant_admin's forced scope survives a spoofed tenant_id in the
    /ui/select-tenant form field.
  * super_admin cannot delete the account they're logged in as (lockout
    guard — env-var break-glass would still work but that's not obvious
    to a panicked operator).
  * env-var admin still authenticates even when admin_users has other rows.
"""
from __future__ import annotations

import asyncio
import uuid

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.auth import sign_session
from docgen.main import app
from docgen.store.db import AsyncSessionLocal, async_engine, sync_session
from docgen.store.models import AdminUser, Template, Tenant


def _hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


@pytest.fixture
def two_tenants_and_admins():
    """Seed tenant A + tenant B, and a tenant_admin pinned to tenant A.

    Yields (tenant_a_id, tenant_b_id, tenant_a_admin_username).
    """
    a_id = uuid.uuid4()
    b_id = uuid.uuid4()
    a_admin = f"a-admin-{uuid.uuid4().hex[:6]}"
    with sync_session() as s:
        s.add(Tenant(id=a_id, name=f"tenant-a-{a_id.hex[:6]}"))
        s.add(Tenant(id=b_id, name=f"tenant-b-{b_id.hex[:6]}"))
        s.flush()
        s.add(AdminUser(
            tenant_id=a_id,
            username=a_admin,
            password_hash=_hash("testpass"),
            role="tenant_admin",
            is_active=True,
        ))
        s.commit()

    yield (a_id, b_id, a_admin)

    with sync_session() as s:
        row = s.execute(
            select(AdminUser).where(AdminUser.username == a_admin)
        ).scalar_one_or_none()
        if row is not None:
            s.delete(row)
        for tid in (a_id, b_id):
            t = s.get(Tenant, tid)
            if t is not None:
                s.delete(t)
        s.commit()


def _tenant_admin_client(username: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("docgen_session", sign_session(username))
    return client


def _super_admin_client() -> TestClient:
    """Env-var super_admin — matches conftest's DOCGEN_ADMIN_USER default."""
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("docgen_session", sign_session("admin"))
    return client


# ---------------------------------------------------------------------------
# super_admin-only routes reject tenant_admin
# ---------------------------------------------------------------------------

def test_tenant_admin_cannot_list_all_tenants(two_tenants_and_admins) -> None:
    _a, _b, a_admin = two_tenants_and_admins
    with _tenant_admin_client(a_admin) as client:
        r = client.get("/ui/admin/tenants")
    assert r.status_code == 404


def test_tenant_admin_cannot_open_admin_users(two_tenants_and_admins) -> None:
    _a, _b, a_admin = two_tenants_and_admins
    with _tenant_admin_client(a_admin) as client:
        r = client.get("/ui/admin/users")
    assert r.status_code == 404


def test_super_admin_can_open_admin_users() -> None:
    with _super_admin_client() as client:
        r = client.get("/ui/admin/users")
    assert r.status_code == 200
    assert "Portal users" in r.text or "admin users" in r.text.lower()


def test_super_admin_can_open_admin_tenants() -> None:
    with _super_admin_client() as client:
        r = client.get("/ui/admin/tenants")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Cross-tenant template detail — tenant_admin blocked with 404
# ---------------------------------------------------------------------------

def _seed_template(tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        try:
            async with AsyncSessionLocal() as s:
                tmpl = Template(name=name, tenant_id=tenant_id)
                s.add(tmpl)
                await s.commit()
                await s.refresh(tmpl)
                return tmpl.id
        finally:
            await async_engine.dispose()

    return asyncio.run(_run())


def test_tenant_admin_cannot_view_other_tenants_template(
    two_tenants_and_admins,
) -> None:
    _a_id, b_id, a_admin = two_tenants_and_admins
    # Seed a template that belongs to Tenant B. Tenant A's admin should
    # see the same 404 they'd see for a completely non-existent template.
    b_template_id = _seed_template(b_id, f"b-only-{uuid.uuid4().hex[:6]}")

    try:
        with _tenant_admin_client(a_admin) as client:
            r = client.get(f"/ui/templates/{b_template_id}/schema")
        assert r.status_code == 404
    finally:
        with sync_session() as s:
            t = s.get(Template, b_template_id)
            if t is not None:
                s.delete(t)
                s.commit()


def test_tenant_admin_can_view_own_tenants_template(
    two_tenants_and_admins,
) -> None:
    """Sanity check — the scope guard doesn't block the tenant_admin from
    seeing their own resources. 404 here would mean the guard is too tight."""
    a_id, _b, a_admin = two_tenants_and_admins
    a_template_id = _seed_template(a_id, f"a-only-{uuid.uuid4().hex[:6]}")

    try:
        with _tenant_admin_client(a_admin) as client:
            r = client.get(f"/ui/templates/{a_template_id}/schema")
        # No frozen version exists, so schema route responds with a "no
        # frozen version" HTML (200) rather than 404. Either way, the scope
        # check let us past.
        assert r.status_code != 404, r.text[:400]
    finally:
        with sync_session() as s:
            t = s.get(Template, a_template_id)
            if t is not None:
                s.delete(t)
                s.commit()


# ---------------------------------------------------------------------------
# Sidebar workspace picker is immovable for tenant_admin
# ---------------------------------------------------------------------------

def test_tenant_admin_cannot_switch_to_another_tenant(
    two_tenants_and_admins,
) -> None:
    """POSTing tenant B's id to /ui/select-tenant when signed in as tenant A's
    admin must NOT set a cookie scoping to B. The route silently coerces the
    id back to the tenant_admin's own tenant."""
    a_id, b_id, a_admin = two_tenants_and_admins
    with _tenant_admin_client(a_admin) as client:
        # CSRF: /ui/select-tenant requires a CSRF cookie + form field. Grab
        # them by first visiting any /ui page that mints the cookie.
        client.get("/ui/")
        csrf = client.cookies.get("docgen_csrf")
        assert csrf, "expected /ui/ to set docgen_csrf cookie"
        r = client.post(
            "/ui/select-tenant",
            data={"tenant_id": str(b_id), "csrf_token": csrf},
        )
    # Route redirects on success (303). The workspace cookie should be either
    # unset (route rejected the swap) or scoped to Tenant A (coerced to
    # tenant_admin's own tenant).
    assert r.status_code in (303, 302)
    cookie = r.cookies.get("docgen_ui_tenant")
    if cookie is not None:
        assert cookie == str(a_id), (
            f"tenant_admin should not be able to scope to another tenant; got {cookie}"
        )


# ---------------------------------------------------------------------------
# Self-delete guard
# ---------------------------------------------------------------------------

def test_super_admin_cannot_delete_own_account() -> None:
    """A super_admin logged in as user X POSTing delete for their own row
    must be refused — even though they'd otherwise have permission."""
    username = f"selfdel-{uuid.uuid4().hex[:6]}"
    with sync_session() as s:
        u = AdminUser(
            tenant_id=None,
            username=username,
            password_hash=_hash("testpass"),
            role="super_admin",
            is_active=True,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        user_id = u.id

    try:
        client = TestClient(app, follow_redirects=False)
        client.cookies.set("docgen_session", sign_session(username))
        # Mint CSRF cookie.
        client.get("/ui/admin/users")
        csrf = client.cookies.get("docgen_csrf")
        assert csrf, "expected admin_users page to set docgen_csrf"
        r = client.post(
            f"/ui/admin/users/{user_id}/delete",
            data={"csrf_token": csrf},
        )
        # Route redirects with a flash error; row must still exist.
        assert r.status_code in (303, 302)
        with sync_session() as s:
            still_there = s.get(AdminUser, user_id)
            assert still_there is not None, "self-delete guard failed — row was removed"
    finally:
        with sync_session() as s:
            leftover = s.get(AdminUser, user_id)
            if leftover is not None:
                s.delete(leftover)
                s.commit()


# ---------------------------------------------------------------------------
# Env-var break-glass keeps working after DB users exist
# ---------------------------------------------------------------------------

def test_env_admin_still_authenticates_when_db_users_exist() -> None:
    """Once real users exist in admin_users, the env-var identity ('admin' in
    tests, per conftest) must still resolve as super_admin — otherwise a
    wedged DB row could lock the operator out.

    Note: the first-boot seeder in main._startup_checks may have already
    written an 'admin' row from the env-var creds. We evict it up-front so
    this test exercises the env-var break-glass path exclusively; the
    seeder is idempotent, so future startups will reseed if needed.
    """
    from docgen.auth import resolve_admin_identity

    # Drop any pre-seeded 'admin' row so we exercise the env-var branch.
    with sync_session() as s:
        existing = s.execute(
            select(AdminUser).where(AdminUser.username == "admin")
        ).scalar_one_or_none()
        if existing is not None:
            s.delete(existing)
            s.commit()

    # Guarantee at least one DB row exists so the "table is empty" fast-path
    # in resolve_admin_identity isn't the reason env-var still works.
    filler = f"filler-{uuid.uuid4().hex[:6]}"
    with sync_session() as s:
        s.add(AdminUser(
            tenant_id=None,
            username=filler,
            password_hash=_hash("testpass"),
            role="super_admin",
            is_active=True,
        ))
        s.commit()

    try:
        async def _resolve():
            try:
                async with AsyncSessionLocal() as s:
                    return await resolve_admin_identity(s, "admin")
            finally:
                await async_engine.dispose()

        ident = asyncio.run(_resolve())
        assert ident is not None, "env-var admin should still resolve"
        assert ident.role == "super_admin"
        assert ident.tenant_id is None
        assert ident.user_id is None  # marker that this came from env, not DB
    finally:
        with sync_session() as s:
            row = s.execute(
                select(AdminUser).where(AdminUser.username == filler)
            ).scalar_one_or_none()
            if row is not None:
                s.delete(row)
                s.commit()
