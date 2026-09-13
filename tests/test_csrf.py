"""CSRF protection on /ui/* mutating routes (H9).

Double-submit cookie: the `docgen_csrf` cookie is compared against the
`csrf_token` form field (or `X-CSRF-Token` header). Safe methods (GET) and
the `/ui/login` endpoint are exempt.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from docgen.auth import CSRF_COOKIE, SESSION_COOKIE, sign_session
from docgen.main import app


def _admin_client_with_csrf(token: str = "test-token") -> TestClient:
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, sign_session("admin"))
    client.cookies.set(CSRF_COOKIE, token)
    return client


def test_post_without_csrf_token_is_rejected() -> None:
    with _admin_client_with_csrf() as client:
        # POST /ui/select-tenant is a cheap authenticated endpoint that reads
        # only form data (no DB write matters for this test — the CSRF check
        # runs before the handler).
        r = client.post("/ui/select-tenant", data={"tenant_id": "", "next": "/ui/"})
    assert r.status_code == 403
    assert "csrf" in r.json()["detail"].lower()


def test_post_with_wrong_csrf_token_is_rejected() -> None:
    with _admin_client_with_csrf(token="cookie-value") as client:
        r = client.post(
            "/ui/select-tenant",
            data={"tenant_id": "", "next": "/ui/", "csrf_token": "wrong-value"},
        )
    assert r.status_code == 403


def test_post_with_matching_csrf_token_is_accepted() -> None:
    with _admin_client_with_csrf(token="matching-value") as client:
        r = client.post(
            "/ui/select-tenant",
            data={"tenant_id": "", "next": "/ui/", "csrf_token": "matching-value"},
            follow_redirects=False,
        )
    # select-tenant redirects on success; anything but 403 confirms the check passed.
    assert r.status_code != 403


def test_post_with_matching_csrf_header_is_accepted() -> None:
    """Header form of the double-submit works for JSON/XHR callers."""
    with _admin_client_with_csrf(token="header-value") as client:
        r = client.post(
            "/ui/select-tenant",
            data={"tenant_id": "", "next": "/ui/"},
            headers={"X-CSRF-Token": "header-value"},
            follow_redirects=False,
        )
    assert r.status_code != 403


def test_login_post_is_exempt_from_csrf() -> None:
    """No session cookie yet, so login must not require CSRF."""
    with TestClient(app, follow_redirects=False) as client:
        # Deliberately wrong credentials — we only care that we get a 401
        # (login handler ran), not 403 (CSRF blocked before handler).
        r = client.post("/ui/login", data={"username": "no", "password": "no"})
    assert r.status_code != 403


def test_get_requests_never_require_csrf() -> None:
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, sign_session("admin"))
    # No CSRF cookie set intentionally.
    with client:
        r = client.get("/ui/upload")
    assert r.status_code == 200


def test_login_sets_csrf_cookie() -> None:
    """Successful login must issue both session and CSRF cookies.

    We patch `get_settings` in both `docgen.ui.routes` (login handler) and
    `docgen.auth` (authenticate_admin) — otherwise authenticate_admin still
    compares against the container's real DOCGEN_ADMIN_PASSWORD_HASH.
    """
    import os
    from unittest.mock import patch

    from docgen.auth import hash_password

    session_secret = os.getenv("DOCGEN_SESSION_SECRET", "change-me-in-prod-please")
    with patch("docgen.ui.routes.get_settings") as gs, \
         patch("docgen.auth.get_settings") as gs_auth:
        for m in (gs, gs_auth):
            m.return_value.admin_user = "admin"
            m.return_value.admin_password_hash = hash_password("s3cret")
            m.return_value.session_secret = session_secret
        with TestClient(app, follow_redirects=False) as client:
            r = client.post("/ui/login", data={"username": "admin", "password": "s3cret"})
    assert r.status_code == 303
    cookies = r.headers.get_list("set-cookie")
    joined = "\n".join(cookies)
    assert SESSION_COOKIE in joined
    assert CSRF_COOKIE in joined
