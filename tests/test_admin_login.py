"""Feature tests for the admin login (/ui/login, /ui/logout, session middleware).

Complements existing coverage:
  - `test_login_ratelimit.py`   — sliding-window rate limiter on POST /ui/login
  - `test_csrf.py`              — CSRF exemption + CSRF cookie issuance at login
  - `test_security_headers.py`  — CSP + Secure/HSTS flags on the login response
  - `test_ui_smoke.py`          — login form renders + protected routes redirect

The gaps closed here are the login *feature surface* itself:
  * form rendering (disabled state, `next` propagation, autocomplete)
  * credential handling (wrong password, unknown user, empty inputs)
  * `next=`-driven redirect + open-redirect guard
  * session cookie attributes (HttpOnly / SameSite=Lax) and round-trip usage
  * middleware kick-outs (tampered / unknown-user / deactivated-user sessions)
  * logout: cookie clearing, method restriction
  * DB-backed admin login: super_admin + tenant_admin paths, last_login_at bump
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from docgen.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    hash_password,
    sign_session,
    verify_session,
)
from docgen.main import app
from docgen.store.db import sync_session
from docgen.store.models import AdminUser, Tenant

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import os

# Per-worker IP subnet so parallel pytest processes don't share Redis
# rate-limit buckets. See test_login_ratelimit.py for the same pattern.
_TEST_IP_SUBNET = f"198.51.{(os.getpid() % 250) + 1}"


def _ip(last_octet: int) -> str:
    return f"{_TEST_IP_SUBNET}.{last_octet}"



def _hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _post_login(
    client: TestClient,
    *,
    username: str = "admin",
    password: str = "testpass",
    next_: str = "",
    ip: str = _ip(100),
):
    """Post /ui/login with a unique-ish IP so parallel tests don't collide on
    the rate-limit bucket (conftest wipes login_rl:* between tests, so this
    is defence in depth)."""
    return client.post(
        "/ui/login",
        data={"username": username, "password": password, "next": next_},
        headers={"x-forwarded-for": ip},
    )


def _patch_admin_settings(*, enabled: bool = True):
    """Patch the login stack's settings snapshot. Both the handler (routes.py)
    and the credential check (auth.py) call `get_settings()` from their own
    module namespace, so we patch both — otherwise the handler would see our
    fake hash but authenticate_admin would still compare against the real one
    baked into the container's DOCGEN_ADMIN_PASSWORD_HASH env var.
    Returns the routes-side mock in case a test wants to tweak it."""
    fake = patch("docgen.ui.routes.get_settings").start().return_value
    fake.admin_user = "admin"
    fake.admin_password_hash = hash_password("testpass") if enabled else ""
    fake.session_secret = "test-session-secret"
    fake.secure_cookies = False
    fake.hsts_enabled = False

    auth_fake = patch("docgen.auth.get_settings").start().return_value
    auth_fake.admin_user = "admin"
    auth_fake.admin_password_hash = hash_password("testpass") if enabled else ""
    auth_fake.session_secret = "test-session-secret"
    return fake


@pytest.fixture
def _trust_xff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rate-limit code ignores X-Forwarded-For unless the peer is a trusted
    proxy; TestClient can't play a trusted proxy easily, so shim it."""
    monkeypatch.setattr("docgen.ratelimit._peer_is_trusted", lambda _peer: True)


@pytest.fixture
def db_super_admin():
    """Seed a DB-backed super_admin distinct from the env-var 'admin' so tests
    hit the DB auth branch of authenticate_admin, not the break-glass path."""
    username = f"sup-{uuid.uuid4().hex[:8]}"
    password = f"pw-{uuid.uuid4().hex[:8]}"
    with sync_session() as s:
        u = AdminUser(
            tenant_id=None,
            username=username,
            password_hash=_hash(password),
            role="super_admin",
            is_active=True,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        user_id = u.id
    yield {"username": username, "password": password, "id": user_id}
    with sync_session() as s:
        row = s.get(AdminUser, user_id)
        if row is not None:
            s.delete(row)
            s.commit()


@pytest.fixture
def db_tenant_admin():
    """Seed a fresh tenant + a tenant_admin pinned to it."""
    tid = uuid.uuid4()
    username = f"ta-{uuid.uuid4().hex[:8]}"
    password = f"pw-{uuid.uuid4().hex[:8]}"
    with sync_session() as s:
        s.add(Tenant(id=tid, name=f"login-t-{tid.hex[:6]}"))
        s.flush()
        u = AdminUser(
            tenant_id=tid,
            username=username,
            password_hash=_hash(password),
            role="tenant_admin",
            is_active=True,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        user_id = u.id
    yield {
        "username": username,
        "password": password,
        "id": user_id,
        "tenant_id": tid,
    }
    with sync_session() as s:
        row = s.get(AdminUser, user_id)
        if row is not None:
            s.delete(row)
        t = s.get(Tenant, tid)
        if t is not None:
            s.delete(t)
        s.commit()


# ---------------------------------------------------------------------------
# GET /ui/login — form rendering
# ---------------------------------------------------------------------------


def test_login_form_shows_disabled_state_when_no_password_configured() -> None:
    """When DOCGEN_ADMIN_PASSWORD_HASH is empty the login GET returns 503 with
    a bootstrap-instructions banner instead of the form. This is the deploy
    story before an operator seeds the env-var admin."""
    try:
        _patch_admin_settings(enabled=False)
        with TestClient(app) as client:
            r = client.get("/ui/login")
        assert r.status_code == 503, r.text[:200]
        assert "Admin portal is not enabled" in r.text
        assert "DOCGEN_ADMIN_PASSWORD_HASH" in r.text
        # Disabled banner must not render the submit form.
        assert 'name="password"' not in r.text
    finally:
        patch.stopall()


def test_login_form_carries_next_param_through_hidden_input() -> None:
    """?next=/ui/upload survives round-tripping via a hidden form field so the
    POST handler can honour it after successful auth."""
    with TestClient(app) as client:
        r = client.get("/ui/login?next=/ui/upload")
    assert r.status_code == 200
    assert 'name="next"' in r.text
    assert 'value="/ui/upload"' in r.text


def test_login_form_has_password_and_username_inputs_with_autocomplete() -> None:
    """Both fields are present with browser-friendly autocomplete tokens so
    password managers can fill correctly."""
    with TestClient(app) as client:
        r = client.get("/ui/login")
    assert r.status_code == 200
    assert 'name="username"' in r.text
    assert 'autocomplete="username"' in r.text
    assert 'name="password"' in r.text
    assert 'autocomplete="current-password"' in r.text
    # POST target is the same route.
    assert 'action="/ui/login"' in r.text
    assert 'method="post"' in r.text


# ---------------------------------------------------------------------------
# POST /ui/login — credential handling
# ---------------------------------------------------------------------------


def test_login_post_returns_503_when_disabled() -> None:
    """A POST arriving while the portal is disabled must not fall through to
    authenticate_admin — return the same 503 banner as GET."""
    try:
        _patch_admin_settings(enabled=False)
        with TestClient(app, follow_redirects=False) as client:
            r = _post_login(client, username="admin", password="testpass")
        assert r.status_code == 503, r.text[:200]
        assert "Admin portal is not enabled" in r.text
    finally:
        patch.stopall()


def test_login_post_with_wrong_password_returns_401_with_error(_trust_xff) -> None:
    """The env-var admin exists (see conftest) but the password is wrong ->
    401 and the flash message is rendered into the response body."""
    with TestClient(app, follow_redirects=False) as client:
        r = _post_login(
            client, username="admin", password="wrong", ip=_ip(101)
        )
    assert r.status_code == 401, r.text[:200]
    assert "Invalid credentials" in r.text


def test_login_post_with_unknown_username_returns_401(_trust_xff) -> None:
    with TestClient(app, follow_redirects=False) as client:
        r = _post_login(
            client,
            username=f"nobody-{uuid.uuid4().hex[:6]}",
            password="testpass",
            ip=_ip(102),
        )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


def test_login_post_with_empty_password_is_rejected(_trust_xff) -> None:
    """Empty password must never authenticate. Either the framework rejects it
    at Form(...) parse time (422) or the handler rejects the empty string at
    authenticate_admin (401) — both are acceptable outcomes; a 3xx or 200
    would be a regression."""
    with TestClient(app, follow_redirects=False) as client:
        r = _post_login(
            client, username="admin", password="", ip=_ip(103)
        )
    assert r.status_code in (401, 422), r.text[:200]


def test_login_post_with_empty_username_is_rejected(_trust_xff) -> None:
    """Same story as the empty-password case: 401 or 422, never a success."""
    with TestClient(app, follow_redirects=False) as client:
        r = _post_login(
            client, username="", password="testpass", ip=_ip(104)
        )
    assert r.status_code in (401, 422), r.text[:200]


def test_failed_login_preserves_next_in_rendered_form(_trust_xff) -> None:
    """After a bad-credentials 401, the retry form still carries the caller's
    original `next` so the eventual retry lands where they were going."""
    with TestClient(app, follow_redirects=False) as client:
        r = _post_login(
            client,
            username="admin",
            password="wrong",
            next_="/ui/upload",
            ip=_ip(105),
        )
    assert r.status_code == 401
    assert 'value="/ui/upload"' in r.text


# ---------------------------------------------------------------------------
# POST /ui/login — success + `next` redirect handling
# ---------------------------------------------------------------------------


def test_login_success_redirects_to_dashboard_by_default(_trust_xff) -> None:
    """No `next` supplied -> land on /ui/dashboard, the canonical logged-in
    home. Patches the handler's settings snapshot because the container's
    real DOCGEN_ADMIN_PASSWORD_HASH isn't our test 'testpass'."""
    try:
        _patch_admin_settings(enabled=True)
        with TestClient(app, follow_redirects=False) as client:
            r = _post_login(client, ip=_ip(110))
        assert r.status_code == 303, r.text[:200]
        assert r.headers["location"] == "/ui/dashboard"
    finally:
        patch.stopall()


def test_login_success_redirects_to_next_when_safe_path(_trust_xff) -> None:
    """A relative next=/ui/... is honoured verbatim."""
    try:
        _patch_admin_settings(enabled=True)
        with TestClient(app, follow_redirects=False) as client:
            r = _post_login(
                client, next_="/ui/upload", ip=_ip(111)
            )
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/upload"
    finally:
        patch.stopall()


def test_login_success_rejects_absolute_url_next(_trust_xff) -> None:
    """Open-redirect guard: `next` must start with '/'. An absolute URL is
    dropped and we fall back to the dashboard."""
    try:
        _patch_admin_settings(enabled=True)
        with TestClient(app, follow_redirects=False) as client:
            r = _post_login(
                client, next_="http://evil.example/steal", ip=_ip(112)
            )
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/dashboard"
    finally:
        patch.stopall()


def test_login_sets_signed_httponly_lax_session_cookie(_trust_xff) -> None:
    """Session cookie shape: value is HMAC-signed (verify_session decodes it),
    HttpOnly + SameSite=Lax attrs are present."""
    try:
        _patch_admin_settings(enabled=True)
        with TestClient(app, follow_redirects=False) as client:
            r = _post_login(client, ip=_ip(113))
        assert r.status_code == 303
        cookies = r.headers.get_list("set-cookie")
        session_cookie = next(
            (c for c in cookies if c.startswith(f"{SESSION_COOKIE}=")), None
        )
        assert session_cookie is not None, cookies
        assert "httponly" in session_cookie.lower()
        assert "samesite=lax" in session_cookie.lower()
        # Round-trip the value: the signed payload must decode to our username.
        raw = session_cookie.split(";", 1)[0].split("=", 1)[1]
        assert verify_session(raw) == "admin"
    finally:
        patch.stopall()


# ---------------------------------------------------------------------------
# Session cookie + AdminSessionMiddleware
# ---------------------------------------------------------------------------


def test_valid_session_cookie_grants_access_to_protected_route(_trust_xff) -> None:
    """End-to-end: log in, then reuse the returned cookie to fetch a gated
    page. Anything but 200 means the session round-trip broke."""
    try:
        _patch_admin_settings(enabled=True)
        with TestClient(app, follow_redirects=False) as client:
            r_login = _post_login(client, ip=_ip(120))
            assert r_login.status_code == 303
            r_dash = client.get("/ui/dashboard")
        assert r_dash.status_code == 200
        assert "Dashboard" in r_dash.text
    finally:
        patch.stopall()


def test_tampered_session_cookie_kicks_to_login() -> None:
    """A garbage session cookie fails signature verification -> redirected to
    /ui/login. The middleware does not raise."""
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, "not-a-valid-signed-cookie")
    with client:
        r = client.get("/ui/dashboard")
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


def test_session_for_unknown_user_clears_cookie_and_redirects() -> None:
    """A cryptographically valid session cookie for a username that neither
    matches the env-var admin nor any DB row must not authenticate; the
    middleware clears the cookie to prevent replay after impersonation."""
    ghost = f"ghost-{uuid.uuid4().hex[:8]}"
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, sign_session(ghost))
    with client:
        r = client.get("/ui/dashboard")
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]
    # Middleware issues a Set-Cookie that expires the session cookie.
    joined = "\n".join(r.headers.get_list("set-cookie")).lower()
    assert SESSION_COOKIE in joined
    assert ("max-age=0" in joined) or ("expires=" in joined)


def test_deactivated_user_session_is_kicked_to_login(db_super_admin) -> None:
    """A DB user who was logged in and then deactivated (is_active=False) must
    be booted on the next request even if their session cookie is unexpired."""
    with sync_session() as s:
        u = s.get(AdminUser, db_super_admin["id"])
        u.is_active = False
        s.commit()

    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, sign_session(db_super_admin["username"]))
    with client:
        r = client.get("/ui/dashboard")
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_post_redirects_to_login_and_clears_cookies() -> None:
    """POST /ui/logout: 303 -> /ui/login, and both session + csrf cookies are
    deleted via expiry headers."""
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, sign_session("admin"))
    client.cookies.set(CSRF_COOKIE, "logout-csrf-value")
    with client:
        r = client.post(
            "/ui/logout",
            headers={"X-CSRF-Token": "logout-csrf-value"},
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
    joined = "\n".join(r.headers.get_list("set-cookie")).lower()
    # Both cookies must be expired (Max-Age=0 or an Expires in the past).
    assert SESSION_COOKIE in joined and CSRF_COOKIE in joined


def test_logout_get_is_method_not_allowed() -> None:
    """Logout is a mutation and must reject GET so a rogue <img src=...> can't
    log the user out (defence in depth; the CSRF cookie check is the primary
    protection)."""
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, sign_session("admin"))
    with client:
        r = client.get("/ui/logout")
    assert r.status_code == 405


# ---------------------------------------------------------------------------
# DB-backed admin login
# ---------------------------------------------------------------------------


def test_db_backed_super_admin_can_login_and_last_login_at_is_stamped(
    db_super_admin, _trust_xff
) -> None:
    """authenticate_admin's primary DB path: an admin_users row with matching
    bcrypt hash authenticates, and last_login_at is bumped as a side effect."""
    with TestClient(app, follow_redirects=False) as client:
        r = _post_login(
            client,
            username=db_super_admin["username"],
            password=db_super_admin["password"],
            ip=_ip(130),
        )
    assert r.status_code == 303, r.text[:200]
    assert r.headers["location"] == "/ui/dashboard"

    with sync_session() as s:
        row = (
            s.execute(
                select(AdminUser).where(
                    AdminUser.username == db_super_admin["username"]
                )
            )
        ).scalar_one()
        assert row.last_login_at is not None
        # Timestamp must be recent (< a minute old); allow any tz.
        delta = datetime.now(UTC) - row.last_login_at.replace(tzinfo=UTC)
        assert delta.total_seconds() < 60


def test_db_backed_tenant_admin_can_login(db_tenant_admin, _trust_xff) -> None:
    """tenant_admin can sign in; the returned session cookie unlocks /ui/
    (the sidebar renders their pinned tenant, but that's a separate RBAC
    concern already covered by test_rbac.py)."""
    with TestClient(app, follow_redirects=False) as client:
        r = _post_login(
            client,
            username=db_tenant_admin["username"],
            password=db_tenant_admin["password"],
            ip=_ip(131),
        )
        assert r.status_code == 303, r.text[:200]
        # Reuse the same client so the session cookie sticks.
        r_dash = client.get("/ui/dashboard")
    assert r_dash.status_code == 200


def test_db_user_wrong_password_returns_401(db_super_admin, _trust_xff) -> None:
    """A DB user + wrong password must not fall back to the env-var admin
    (different username) — it stays 401."""
    with TestClient(app, follow_redirects=False) as client:
        r = _post_login(
            client,
            username=db_super_admin["username"],
            password="definitely-not-the-real-one",
            ip=_ip(132),
        )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text
