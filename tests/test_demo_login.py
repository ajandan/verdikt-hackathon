"""Demo sign-in for judges: the login-page button and the .env-driven account.

The credentials are published on a public page, so the tests that matter are
the ones about what the feature must NOT do: show a button when it is off or
half-configured, keep an account alive after the flag is turned off, or touch
an account that is not its own.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.auth import hash_password, verify_password
from docgen.main import _sync_demo_user, app
from docgen.store.db import async_engine, sync_session
from docgen.store.models import AdminUser, Tenant


def _login_page_settings(*, enabled: bool, username: str = "judge", password: str = "demo-pw-123"):
    fake = patch("docgen.ui.routes.get_settings").start().return_value
    fake.admin_user = "admin"
    fake.admin_password_hash = hash_password("testpass")
    fake.demo_login_enabled = enabled
    fake.demo_username = username
    fake.demo_password = password
    return fake


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


def _get_login() -> str:
    with TestClient(app) as c:
        r = c.get("/ui/login")
    assert r.status_code == 200
    return r.text


# --- login page --------------------------------------------------------------

def test_no_demo_panel_when_disabled() -> None:
    _login_page_settings(enabled=False)
    html = _get_login()
    assert 'id="demo-login"' not in html
    assert "demo-pw-123" not in html


def test_demo_panel_shows_credentials_and_button_when_enabled() -> None:
    _login_page_settings(enabled=True)
    html = _get_login()
    assert 'id="demo-login"' in html
    assert "judge" in html and "demo-pw-123" in html
    # The fill script must carry the CSP nonce or the policy blocks it.
    assert '<script nonce="' in html
    # Nonce-protected page: inline style attributes are silently dropped, so
    # the panel must be styled by class only.
    panel = html[html.index('class="demo-access"'):html.index("</form>")]
    assert ' style="' not in panel


@pytest.mark.parametrize("username,password", [("judge", ""), ("", "demo-pw-123")])
def test_half_configured_demo_shows_no_button(username: str, password: str) -> None:
    _login_page_settings(enabled=True, username=username, password=password)
    assert 'id="demo-login"' not in _get_login()


# --- provisioning from .env --------------------------------------------------

@pytest.fixture
def demo_tenant():
    tid = uuid.uuid4()
    name = f"demo-t-{tid.hex[:6]}"
    with sync_session() as s:
        s.add(Tenant(id=tid, name=name))
        s.commit()
    yield {"id": tid, "name": name}
    with sync_session() as s:
        for row in s.execute(select(AdminUser).where(AdminUser.tenant_id == tid)).scalars():
            s.delete(row)
        t = s.get(Tenant, tid)
        if t is not None:
            s.delete(t)
        s.commit()


def _sync(**settings) -> None:
    fake = patch("docgen.main.get_settings").start().return_value
    fake.admin_user = settings.pop("admin_user", "admin")
    for k, v in settings.items():
        setattr(fake, k, v)
    asyncio.run(async_engine.dispose())          # pool must belong to this loop
    asyncio.run(_sync_demo_user())
    asyncio.run(async_engine.dispose())
    patch.stopall()


def _row(username: str) -> AdminUser | None:
    with sync_session() as s:
        row = s.execute(select(AdminUser).where(AdminUser.username == username)).scalar_one_or_none()
        if row is not None:
            s.expunge(row)
        return row


def test_enabled_creates_a_tenant_admin_with_the_env_password(demo_tenant) -> None:
    name = f"judge-{uuid.uuid4().hex[:6]}"
    _sync(demo_login_enabled=True, demo_username=name, demo_password="pw-one",
          demo_tenant=demo_tenant["name"])
    row = _row(name)
    assert row is not None
    assert row.role == "tenant_admin"
    assert row.tenant_id == demo_tenant["id"]
    assert row.is_active
    assert verify_password("pw-one", row.password_hash)


def test_changing_the_env_password_rotates_it(demo_tenant) -> None:
    name = f"judge-{uuid.uuid4().hex[:6]}"
    base = dict(demo_login_enabled=True, demo_username=name, demo_tenant=str(demo_tenant["id"]))
    _sync(demo_password="pw-one", **base)
    _sync(demo_password="pw-two", **base)
    row = _row(name)
    assert verify_password("pw-two", row.password_hash)
    assert not verify_password("pw-one", row.password_hash)


def test_disabling_deactivates_the_account(demo_tenant) -> None:
    name = f"judge-{uuid.uuid4().hex[:6]}"
    _sync(demo_login_enabled=True, demo_username=name, demo_password="pw-one",
          demo_tenant=demo_tenant["name"])
    _sync(demo_login_enabled=False, demo_username=name, demo_password="pw-one",
          demo_tenant=demo_tenant["name"])
    assert _row(name).is_active is False


def test_refuses_to_reuse_the_env_admin_name(demo_tenant) -> None:
    _sync(admin_user="boss", demo_login_enabled=True, demo_username="boss",
          demo_password="pw", demo_tenant=demo_tenant["name"])
    assert _row("boss") is None


def test_never_touches_an_existing_super_admin(demo_tenant) -> None:
    name = f"sup-{uuid.uuid4().hex[:6]}"
    with sync_session() as s:
        s.add(AdminUser(tenant_id=None, username=name, password_hash=hash_password("real-pw"),
                        role="super_admin", is_active=True))
        s.commit()
    try:
        _sync(demo_login_enabled=True, demo_username=name, demo_password="published",
              demo_tenant=demo_tenant["name"])
        row = _row(name)
        assert row.role == "super_admin"
        assert row.tenant_id is None
        assert verify_password("real-pw", row.password_hash)
        assert not verify_password("published", row.password_hash)
        # and turning the demo off must not lock a real admin out either
        _sync(demo_login_enabled=False, demo_username=name, demo_password="published",
              demo_tenant=demo_tenant["name"])
        assert _row(name).is_active is True
    finally:
        with sync_session() as s:
            row = s.execute(select(AdminUser).where(AdminUser.username == name)).scalar_one_or_none()
            if row is not None:
                s.delete(row)
                s.commit()


def test_unknown_tenant_creates_nothing() -> None:
    name = f"judge-{uuid.uuid4().hex[:6]}"
    _sync(demo_login_enabled=True, demo_username=name, demo_password="pw",
          demo_tenant="no such tenant")
    assert _row(name) is None
